from diffusion_onnx import GaussianDiffusion
import os
import yaml
import torch
import torch.nn as nn
import numpy as np
from wavenet import WaveNet
import torch.nn.functional as F
import diffusion
import json
import argparse

parser = argparse.ArgumentParser(description='Onnx Export')
parser.add_argument('--project', type=str, help='Project Name')
args_main = parser.parse_args()

class DotDict(dict):
    def __getattr__(*args):         
        val = dict.get(*args)         
        return DotDict(val) if type(val) is dict else val   

    __setattr__ = dict.__setitem__    
    __delattr__ = dict.__delitem__

    
def load_model_vocoder(
        model_path,
        device='cpu'):
    config_file = model_path + '/config.yaml'
    model_path = model_path + '/model.pt'
    with open(config_file, "r") as config:
        args = yaml.safe_load(config)
    args = DotDict(args)
    
    # load model
    model = Unit2Mel(
                args.data.encoder_out_channels, 
                args.model.n_spk,
                args.model.use_pitch_aug,
                128,
                args.model.n_layers,
                args.model.n_chans,
                args.model.n_hidden,
                args.data.encoder_hop_size,
                args.data.sampling_rate)
    
    print(' [Loading] ' + model_path)
    ckpt = torch.load(model_path, map_location=torch.device(device))
    model.to(device)
    model.load_state_dict(ckpt['model'])
    model.eval()
    return model, args


class Unit2Mel(nn.Module):
    def __init__(
            self,
            input_channel,
            n_spk,
            use_pitch_aug=False,
            out_dims=128,
            n_layers=20, 
            n_chans=384, 
            n_hidden=256,
            hop_size=320,
            sampling_rate=44100,
            use_speaker_encoder=False,
            speaker_encoder_out_channels=256):
        super().__init__()
        self.sampling_rate = sampling_rate
        self.hop_size = hop_size
        self.hubert_channel = input_channel
        self.unit_embed = nn.Linear(input_channel, n_hidden)
        self.f0_embed = nn.Linear(1, n_hidden)
        self.volume_embed = nn.Linear(1, n_hidden)
        if use_pitch_aug:
            self.aug_shift_embed = nn.Linear(1, n_hidden, bias=False)
        else:
            self.aug_shift_embed = None
        self.n_spk = n_spk
        self.use_speaker_encoder = use_speaker_encoder
        if use_speaker_encoder:
            self.spk_embed = nn.Linear(speaker_encoder_out_channels, n_hidden, bias=False)
        else:
            if n_spk is not None and n_spk > 1:
                self.spk_embed = nn.Embedding(n_spk, n_hidden)
            
        # diffusion
        self.decoder = GaussianDiffusion(out_dims, n_layers, n_chans, n_hidden)
        self.hidden_size = n_hidden
        

    def forward(self, units, mel2ph, f0, volume, g = None):
        
        '''
        input: 
            B x n_frames x n_unit
        return: 
            dict of B x n_frames x feat
        '''

        decoder_inp = F.pad(units, [0, 0, 1, 0])
        mel2ph_ = mel2ph.unsqueeze(2).repeat([1, 1, units.shape[-1]])
        units = torch.gather(decoder_inp, 1, mel2ph_)  # [B, T, H]

        x = self.unit_embed(units) + self.f0_embed((1 + f0.unsqueeze(-1) / 700).log()) + self.volume_embed(volume.unsqueeze(-1))

        if self.n_spk is not None and self.n_spk > 1:   # [N, S]  *  [S, B, 1, H]
            g = g.reshape((g.shape[0], g.shape[1], 1, 1, 1))  # [N, S, B, 1, 1]
            g = g * self.speaker_map  # [N, S, B, 1, H]
            g = torch.sum(g, dim=1) # [N, 1, B, 1, H]
            g = g.transpose(0, -1).transpose(0, -2).squeeze(0) # [B, H, N]
            x = x.transpose(1, 2) + g
            return x
        else:
            return x.transpose(1, 2)
        

    def init_spkembed(self, units, f0, volume, spk_id = None, spk_mix_dict = None, aug_shift = None,
                gt_spec=None, infer=True, infer_speedup=10, method='dpm-solver', k_step=300, use_tqdm=True):
        
        '''
        input: 
            B x n_frames x n_unit
        return: 
            dict of B x n_frames x feat
        '''
        spk_dice_offset = 0
        self.speaker_map = torch.zeros((self.n_spk,1,1,self.hidden_size))

        if self.use_speaker_encoder:
            if spk_mix_dict is not None:
                assert spk_emb_dict is not None
                for k, v in spk_mix_dict.items():
                    spk_id_torch = spk_emb_dict[str(k)]
                    spk_id_torch = np.tile(spk_id_torch, (len(units), 1))
                    spk_id_torch = torch.from_numpy(spk_id_torch).float().to(units.device)
                    self.speaker_map[spk_dice_offset] = self.spk_embed(spk_id_torch)
                    spk_dice_offset = spk_dice_offset + 1
        else:
            if self.n_spk is not None and self.n_spk > 1:
                if spk_mix_dict is not None:
                    for k, v in spk_mix_dict.items():
                        spk_id_torch = torch.LongTensor(np.array([[k]])).to(units.device)
                        self.speaker_map[spk_dice_offset] = self.spk_embed(spk_id_torch)
                        spk_dice_offset = spk_dice_offset + 1
        self.speaker_map = self.speaker_map.unsqueeze(0)
        self.speaker_map = self.speaker_map.detach()



    def OnnxExport(self, project_name=None, init_noise=None, export_encoder=True, export_denoise=True, export_pred=True, export_after=True):
        n_frames = 100
        hubert = torch.randn((1, n_frames, self.hubert_channel))
        mel2ph = torch.arange(end=n_frames).unsqueeze(0).long()
        f0 = torch.randn((1, n_frames))
        volume = torch.randn((1, n_frames))
        spk_mix = []
        spks = {}
        if self.n_spk is not None and self.n_spk > 1:
            for i in range(self.n_spk):
                spk_mix.append(1.0/float(self.n_spk))
                spks.update({i:1.0/float(self.n_spk)})
        spk_mix = torch.tensor(spk_mix)
        spk_mix = spk_mix.repeat(n_frames, 1)
        self.init_spkembed(hubert, f0.unsqueeze(-1), volume.unsqueeze(-1), spk_mix_dict=spks)
        if export_encoder:
            torch.onnx.export(
                self,
                (hubert, mel2ph, f0, volume, spk_mix),
                f"exp/{project_name}/{project_name}_encoder.onnx",
                input_names=["hubert", "mel2ph", "f0", "volume", "spk_mix"],
                output_names=["mel_pred"],
                dynamic_axes={
                    "hubert": [1],
                    "f0": [1],
                    "volume": [1],
                    "mel2ph": [1],
                    "spk_mix": [0],
                },
                opset_version=16
            )
        self.decoder.OnnxExport(project_name, init_noise=init_noise, export_denoise=export_denoise, export_pred=export_pred, export_after=export_after)
        
        vec_lay = "layer-12" if self.hubert_channel == 768 else "layer-9"
        spklist = []
        for key in range(self.n_spk):
            spklist.append(f"Speaker_{key}")

        MoeVSConf = {
            "Folder" : f"{project_name}",
            "Name" : f"{project_name}",
            "Type" : "DiffSvc",
            "Rate" : self.sampling_rate,
            "Hop" : self.hop_size,
            "Hubert": f"vec-{self.hubert_channel}-{vec_lay}",
            "HiddenSize": self.hubert_channel,
            "Characters": spklist,
            "Diffusion": True,
            "CharaMix": True,
            "Volume": True,
            "V2" : True
        }

        MoeVSConfJson = json.dumps(MoeVSConf)
        with open(f"exp/{project_name}/{project_name}.json", 'w') as MoeVsConfFile:
            json.dump(MoeVSConf, MoeVsConfFile, indent = 4)


    def ExportOnnx(self, project_name=None):
        n_frames = 100
        hubert = torch.randn((1, n_frames, self.hubert_channel))
        mel2ph = torch.arange(end=n_frames).unsqueeze(0).long()
        f0 = torch.randn((1, n_frames))
        volume = torch.randn((1, n_frames))
        spk_mix = []
        spks = {}
        if self.n_spk is not None and self.n_spk > 1:
            for i in range(self.n_spk):
                spk_mix.append(1.0/float(self.n_spk))
                spks.update({i:1.0/float(self.n_spk)})
        spk_mix = torch.tensor(spk_mix)
        orgouttt = self.orgforward(hubert, f0.unsqueeze(-1), volume.unsqueeze(-1), spk_mix_dict=spks)
        outtt = self.forward(hubert, mel2ph, f0, volume, spk_mix)

        torch.onnx.export(
                self,
                (hubert, mel2ph, f0, volume, spk_mix),
                f"{project_name}_encoder.onnx",
                input_names=["hubert", "mel2ph", "f0", "volume", "spk_mix"],
                output_names=["mel_pred"],
                dynamic_axes={
                    "hubert": [1],
                    "f0": [1],
                    "volume": [1],
                    "mel2ph": [1]
                },
                opset_version=16
            )

        condition = torch.randn(1,self.decoder.n_hidden,n_frames)
        noise = torch.randn((1, 1, self.decoder.mel_bins, condition.shape[2]), dtype=torch.float32)
        pndm_speedup = torch.LongTensor([100])
        K_steps = torch.LongTensor([1000])
        self.decoder = torch.jit.script(self.decoder)
        self.decoder(condition, noise, pndm_speedup, K_steps)

        torch.onnx.export(
                self.decoder,
                (condition, noise, pndm_speedup, K_steps),
                f"{project_name}_diffusion.onnx",
                input_names=["condition", "noise", "pndm_speedup", "K_steps"],
                output_names=["mel"],
                dynamic_axes={
                    "condition": [2],
                    "noise": [3],
                },
                opset_version=16
            )


if __name__ == "__main__":
    
    project_name = args_main.project
    model_path = f'exp/{project_name}'

    model, _ = load_model_vocoder(model_path)

    # 分开Diffusion导出（需要使用MoeSS/MoeVoiceStudio或者自己编写Pndm/Dpm采样）
    model.OnnxExport(project_name, export_encoder=True, export_denoise=True, export_pred=True, export_after=True)


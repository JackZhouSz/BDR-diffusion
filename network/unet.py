import torch
import torch.nn as nn
from network.model_utils import *
from utils.utils import default, VIT_FEATURE_CHANNEL, CLIP_FEATURE_CHANNEL


class UNetModel(nn.Module):
    def __init__(self,
                 image_size: int = 64,
                 base_channels: int = 64,
                 dim_mults=(1, 2, 4, 8),
                 dropout: float = 0.1,
                 num_heads: int = 1,
                 world_dims: int = 3,
                 attention_resolutions=(4, 8),
                 with_attention: bool = False,
                 verbose: bool = False,
                 image_condition_dim: int = VIT_FEATURE_CHANNEL,
                 text_condition_dim: int = CLIP_FEATURE_CHANNEL,
                 kernel_size: float = 1.0,
                 vit_global: bool = False,
                 vit_local: bool = True,
                 ):
        super().__init__()
        channels = [base_channels, *
                    map(lambda m: base_channels * m, dim_mults)]
        in_out = list(zip(channels[:-1], channels[1:]))

        self.verbose = verbose
        emb_dim = base_channels * 4

        self.time_pos_emb = LearnedSinusoidalPosEmb(base_channels)
        self.time_emb = nn.Sequential(
            nn.Linear(base_channels + 1, emb_dim),
            activation_function(),
            nn.Linear(emb_dim, emb_dim)
        )
        self.cond_pos_emb0 = LearnedSinusoidalPosEmb1(base_channels)
        self.cond_emb0 = nn.Sequential(
            nn.Linear(base_channels + 1, emb_dim),
            activation_function(),
            nn.Linear(emb_dim, emb_dim)
        )
        self.cond_pos_emb1 = LearnedSinusoidalPosEmb1(base_channels)
        self.cond_emb1 = nn.Sequential(
            nn.Linear(base_channels + 1, emb_dim),
            activation_function(),
            nn.Linear(emb_dim, emb_dim)
        )
        self.cond_pos_emb2 = LearnedSinusoidalPosEmb1(base_channels)
        self.cond_emb2 = nn.Sequential(
            nn.Linear(base_channels + 1, emb_dim),
            activation_function(),
            nn.Linear(emb_dim, emb_dim)
        )
        self.null_emb0=nn.Parameter(torch.zeros(emb_dim))
        self.null_emb1=nn.Parameter(torch.zeros(emb_dim))
        self.null_emb2=nn.Parameter(torch.zeros(emb_dim))

        self.input_emb = conv_nd(world_dims, 3, base_channels, 3, padding=1)
        self.downs = nn.ModuleList([])
        self.ups = nn.ModuleList([])
        num_resolutions = len(in_out)
        ds = 1

        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (num_resolutions - 1)
            res = image_size // ds
            use_cross = (res == 4 or res == 8)
            self.downs.append(nn.ModuleList([
                ResnetBlock1(world_dims, dim_in, dim_out,
                            emb_dim=emb_dim, dropout=dropout, ),our_Identity(),
                nn.Sequential(
                    normalization(dim_out),
                    activation_function(),
                    AttentionBlock(
                        dim_out, num_heads=num_heads)) if ds in attention_resolutions and with_attention else our_Identity(),
                Downsample(
                    dim_out, dims=world_dims) if not is_last else our_Identity()
            ]))
            if not is_last:
                ds *= 2

        mid_dim = channels[-1]
        res = image_size // ds
        self.mid_block1 = ResnetBlock1(
            world_dims, mid_dim, mid_dim, emb_dim=emb_dim, dropout=dropout, )
        
        self.mid_cross_attn = our_Identity()
        self.mid_self_attn = nn.Sequential(
            normalization(mid_dim),
            activation_function(),
            AttentionBlock(mid_dim, num_heads=num_heads)
        ) if ds in attention_resolutions and with_attention else our_Identity()
        self.mid_block2 = ResnetBlock1(
            world_dims, mid_dim, mid_dim, emb_dim=emb_dim, dropout=dropout, )

        for ind, (dim_in, dim_out) in enumerate(reversed(in_out[1:])):
            is_last = ind >= (num_resolutions - 1)
            res = image_size // ds
            use_cross = (res == 4 or res == 8)
            self.ups.append(nn.ModuleList([
                ResnetBlock1(world_dims, dim_out * 2, dim_in,
                            emb_dim=emb_dim, dropout=dropout, ),
                our_Identity(),
                nn.Sequential(
                    normalization(dim_in),
                    activation_function(),
                    AttentionBlock(
                        dim_in, num_heads=num_heads)) if ds in attention_resolutions and with_attention else our_Identity(),
                Upsample(
                    dim_in, dims=world_dims) if not is_last else our_Identity()
            ]))
            if not is_last:
                ds //= 2

        self.end = nn.Sequential(
            normalization(base_channels),
            activation_function()
        )

        self.out = conv_nd(world_dims, base_channels, 1, 3, padding=1)

    def forward(self, x, t, img_condition, text_condition, projection_matrix, x_self_cond=None, kernel_size=None, cond=None, bdr=None):

        x_self_cond = default(x_self_cond, lambda: torch.zeros_like(x))
        x = torch.cat((x, x_self_cond, bdr), dim=1)

        if self.verbose:
            print("input size:")
            print(x.shape)

        x = self.input_emb(x)
        t = self.time_emb(self.time_pos_emb(t))

        # 
        null_index=torch.where(cond[:,0]==-1)
        cond_emb0=self.cond_emb0(self.cond_pos_emb0(cond[:,0]))
        cond_emb1=self.cond_emb1(self.cond_pos_emb1(cond[:,1]))
        cond_emb2=self.cond_emb2(self.cond_pos_emb2(cond[:,2]))
        cond_emb0[null_index]=self.null_emb0
        cond_emb1[null_index]=self.null_emb1
        cond_emb2[null_index]=self.null_emb2
        cond_emb=[cond_emb0,cond_emb1,cond_emb2]

        h = []

        for resnet, cross_attn, self_attn, downsample in self.downs:
            x = resnet(x, t, text_condition, cond_emb)
            if self.verbose:
                print(x.shape)
                if type(cross_attn) == CrossAttention:
                    print("cross attention at resolution: ",
                          cross_attn.image_size)
            x = cross_attn(x, img_condition,  projection_matrix, kernel_size)
            x = self_attn(x)
            if self.verbose:
                print(x.shape)
            h.append(x)
            x = downsample(x)
            if self.verbose:
                print(x.shape)

        if self.verbose:
            print("enter bottle neck")
        x = self.mid_block1(x, t, text_condition, cond_emb)
        if self.verbose:
            print(x.shape)

        x = self.mid_cross_attn(
            x, img_condition, projection_matrix, kernel_size)
        x = self.mid_self_attn(x)
        if self.verbose:
            print("cross attention at resolution: ",
                  self.mid_cross_attn.image_size)
            print(x.shape)
        x = self.mid_block2(x, t, text_condition, cond_emb)
        if self.verbose:
            print(x.shape)
            print("finish bottle neck")

        for resnet, cross_attn, self_attn, upsample in self.ups:
            x = torch.cat((x, h.pop()), dim=1)
            if self.verbose:
                print(x.shape)
            x = resnet(x, t, text_condition, cond_emb)
            if self.verbose:
                print(x.shape)
            x = cross_attn(x, img_condition, projection_matrix, kernel_size)
            x = self_attn(x)
            if self.verbose:
                if type(cross_attn) == CrossAttention:
                    print("cross attention at resolution: ",
                          cross_attn.image_size)
                print(x.shape)
            x = upsample(x)
            if self.verbose:
                print(x.shape)

        x = self.end(x)
        if self.verbose:
            print(x.shape)

        return self.out(x)

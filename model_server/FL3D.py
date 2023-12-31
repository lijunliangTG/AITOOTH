from util import cat_tensor, crop_tensor
import torch
import torch.nn as nn
from RESUNet import MinPool

class ResBlock3D(nn.Module):
    def __init__(self, in_channels, out_channels, padding = 1):
        super().__init__()
        self.layer = nn.Sequential(
            nn.Conv3d(in_channels, out_channels, kernel_size = (1,3,3), stride = (1,1,1) , padding = (0,1,1), bias=False),
            nn.BatchNorm3d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv3d(out_channels, out_channels,kernel_size = (1,3,3), stride = (1,1,1), padding = (0,1,1), bias=False),
            nn.BatchNorm3d(out_channels),
            nn.ReLU(inplace=True)
        )

        self.identity_map = nn.Conv3d( in_channels, out_channels, kernel_size = (1,3,3), stride = (1,1,1), padding = (0,1,1))
        self.relu = nn.ReLU(inplace=True)
    def forward(self, inputs):
        x = inputs.clone().detach()
        out = self.layer(x)
        residual  = self.identity_map(inputs)
        skip = out + residual
        return self.relu(skip)

class Hourglass3D(nn.Module):
    def __init__(self, n, f, bn=None, increase=0):
        super(Hourglass3D, self).__init__()
        nf = f + increase
        self.up1 = ResBlock3D(f, f)
        # Lower branch
        self.pool1 = nn.MaxPool3d((1,2,2))
        self.low1 = ResBlock3D(f, nf)
        self.n = n
        # Recursive hourglass3d
        if self.n > 1:
            self.low2 = Hourglass3D(n-1, nf, bn=bn)
        else:
            self.low2 = ResBlock3D(nf, nf)
        self.low3 = ResBlock3D(nf, f)
        self.up2 = nn.Upsample(scale_factor = (1,2,2), mode='nearest')

    def forward(self, x):
        up1  = self.up1(x)
        pool1 = self.pool1(x)
        low1 = self.low1(pool1)
        low2 = self.low2(low1)
        low3 = self.low3(low2)
        up2  = self.up2(low3)
        return up1 + up2

class DShotConnect3D(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv_r = nn.Sequential(
            nn.Conv3d( in_channels, out_channels, kernel_size = (1,3,3), stride = (1,1,1), padding = (0,1,1) ),
            nn.BatchNorm3d(out_channels),
            nn.ReLU(),
        )
        self.conv_l = nn.Sequential(
            nn.Conv3d( in_channels, out_channels, kernel_size = (1,3,3), stride = (1,1,1), padding = (0,1,1) ),
            nn.BatchNorm3d(out_channels),
            nn.ReLU(),
        )
        self.conv = nn.Sequential(
            nn.BatchNorm3d(2 * out_channels),
            nn.Conv3d( 2 * out_channels, out_channels, kernel_size = (1,3,3), stride = (1,1,1), padding = (0,1,1) ),
            nn.ReLU(),
        )
  
    def forward(self, inputs):
        x_r = self.conv_r(inputs)
        x_l = self.conv_l(inputs)
        x = torch.cat([x_r, x_l], dim = 1)
        x = self.conv(x)
        return x


class FL3D(nn.Module):
    def __init__(
                self,
                in_channel = 1,
                out_channel = 1,
                middle_channel = 32, 
                embed_shape = ( 2, 4),
                nstack = 2,
                need_return_dict = False
        ):
        super(FL,self).__init__()
        self.nstack = nstack
        self.embed_shape = embed_shape
        self.need_return_dict = need_return_dict
        
        self.downsample = nn.Sequential(
            nn.AvgPool2d(2),
            nn.Conv2d(in_channel, middle_channel, kernel_size = (3,3), stride = (1,1), padding = 1 )
        )
        
        self.upsample = nn.ConvTranspose2d(out_channel, out_channel, (2,2), (2,2))
        self.erode = MinPool(2,2,1)
        self.dilate = nn.MaxPool2d(2, stride = 1)
        
        self.hgs = nn.ModuleList( [
            nn.Sequential(
                Hourglass3D(4, middle_channel, increase = 32)
            ) for i in range(nstack)] 
        )
        
        self.features = nn.ModuleList( [
                nn.Sequential(
                    ResBlock3D(middle_channel, middle_channel),
                    DShotConnect3D(middle_channel, middle_channel),
                ) for i in range(nstack)
        ])
        
        self.outs = nn.ModuleList( [
            DShotConnect3D(middle_channel, middle_channel)  for i in range(nstack)
        ])
        
        self.merge_features = nn.ModuleList( [
                DShotConnect3D(middle_channel, middle_channel)  for i in range(nstack - 1)
        ] )
        self.merge_preds = nn.ModuleList( [ DShotConnect3D(middle_channel, middle_channel) for i in range(nstack - 1)] )
        self.final = nn.Conv3d( nstack * middle_channel, out_channel, kernel_size = (1,3,3), stride = (1,1,1), padding = (0,1,1) )
        self.relu = nn.ReLU()
        self.sigmod = nn.Sigmoid()
        
        
    
    def get_embeding(self, x):
        embed_x = crop_tensor(x, self.embed_shape[0], self.embed_shape[1])
        embed_x = embed_x.permute(0, 2, 1, 3, 4)
        return embed_x
    
    def re_build(self, x):
        x = x.permute(0, 2, 1, 3, 4)
        x = cat_tensor(x, self.embed_shape[0], self.embed_shape[1])        
        return x

    def build_results(self,x,y):
        return {
            "mask":x,
            'edge':y,
        }
    def forward(self, x):   
        x = self.downsample(x)
        x_embed = self.get_embeding(x)
        combined_hm_preds = []
        for i in range(self.nstack):
            hg = self.hgs[i](x_embed)
            # print("hg:",hg.size()) 
            feature = self.features[i](hg)
            # print("feature:",feature.size())
            preds = self.outs[i](feature)
            keys = self.sigmod(preds)
            # print("preds:", preds.size())
            combined_hm_preds.append( self.relu((preds * hg + feature * hg ) * keys ) )
            if i < self.nstack - 1:
                x_embed = x_embed + self.merge_preds[i](preds) + self.merge_features[i](feature)
        
        x_combine = torch.cat(combined_hm_preds, dim = 1)
        x_embed = self.final(x_combine)
        outp = self.re_build( x_embed )
        outp = self.upsample(outp)
        edge = nn.functional.pad(outp, (1, 0, 1, 0))
        edge = self.dilate(edge) - self.erode(edge)
        return self.build_results(outp, edge) if (self.need_return_dict) else (outp, edge)


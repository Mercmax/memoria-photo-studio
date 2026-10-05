"""Optional isolated Pillow/PyTorch runner. Never downloads weights during inference."""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

LABELS = {'basic':'Бережное улучшение','colorize':'Раскраска DDColor',
          'upscale':'Увеличение Real-ESRGAN','faces':'Восстановление лиц GFPGAN','inpaint':'Ретушь LaMa'}

def probe():
    installed = all(importlib.util.find_spec(m) is not None for m in ('torch','cv2','PIL','realesrgan','gfpgan')) and (Path(__file__).resolve().parent/'vendor'/'DDColor'/'ddcolor'/'__init__.py').is_file()
    result={'installed':installed,'pillow':importlib.util.find_spec('PIL') is not None,
            'devices':['cpu'],'message':'Обработчик установлен' if installed else 'AI-зависимости не установлены'}
    if importlib.util.find_spec('torch'):
        import torch
        result['torch']=torch.__version__
        if torch.cuda.is_available(): result['devices'] += ['cuda:'+str(i) for i in range(torch.cuda.device_count())]
        if hasattr(torch.backends,'mps') and torch.backends.mps.is_available():result['devices'].append('mps')
    print(json.dumps(result))

def compat():
    # BasicSR 1.4.2 imports a module removed by torchvision 0.17+.
    import types
    from torchvision.transforms.functional import rgb_to_grayscale
    shim=types.ModuleType('torchvision.transforms.functional_tensor');shim.rgb_to_grayscale=rgb_to_grayscale
    sys.modules.setdefault('torchvision.transforms.functional_tensor',shim)

def read_rgb(path):
    from PIL import Image, ImageOps, ImageCms
    image=ImageOps.exif_transpose(Image.open(path))
    if image.width*image.height>60_000_000:raise ValueError('Изображение больше 60 Мп')
    profile=image.info.get('icc_profile')
    if profile:
        import io
        try:image=ImageCms.profileToProfile(image,ImageCms.ImageCmsProfile(io.BytesIO(profile)),ImageCms.createProfile('sRGB'),outputMode='RGB')
        except Exception:image=image.convert('RGB')
    return image.convert('RGB')

def save_rgb(image,path,dpi=300):
    from PIL import ImageCms
    profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    kwargs={'icc_profile':profile,'dpi':(dpi,dpi)}
    if path.suffix=='.jpg':kwargs.update(quality=95,subsampling=0)
    if path.suffix=='.tiff':kwargs.update(compression='tiff_lzw')
    image.save(path,**kwargs)

def process(config):
    from PIL import Image,ImageFilter,ImageOps,ImageEnhance
    image=read_rgb(config['source'])
    if image.width*image.height>16_000_000:
        factor=(16_000_000/(image.width*image.height))**.5
        image=image.resize((max(1,int(image.width*factor)),max(1,int(image.height*factor))),Image.Resampling.LANCZOS)
    out=Path(config['output']);models=Path(config['models']);options=config['options'];device=config['device']
    results=[]
    def progress(value,stage):
        temp=out/'progress.tmp';temp.write_text(json.dumps({'progress':value,'stage':stage}),encoding='utf-8');temp.replace(out/'progress.json')
    torch=None
    for index,operation in enumerate(config['operations']):
        progress(int(index/len(config['operations'])*100),LABELS[operation])
        if operation=='basic':
            if options.get('contrast',True):image=Image.blend(image,ImageOps.autocontrast(image,cutoff=1),.25)
            if options.get('denoise',True):image=Image.blend(image,image.filter(ImageFilter.MedianFilter(3)),.2)
            if options.get('sharpen',True):image=image.filter(ImageFilter.UnsharpMask(radius=1,percent=65,threshold=3))
        else:
            if torch is None:
                import torch
                import numpy as np
                import cv2
                compat()
                torch.set_num_threads(4)
                if device.startswith('cuda') and not torch.cuda.is_available():raise ValueError('CUDA недоступна. Проверьте драйвер и установку PyTorch')
            bgr=np.array(image)[:,:,::-1].copy()
            if operation=='colorize':
                sys.path.insert(0,str(Path(__file__).resolve().parent/'vendor'/'DDColor'))
                from ddcolor import DDColor,ColorizationPipeline
                model_id=options.get('color_model','ddcolor')
                model_config=json.loads((models/model_id/'config.json').read_text())
                model=DDColor(**model_config)
                state=torch.load(models/model_id/'pytorch_model.bin',map_location='cpu',weights_only=True)
                model.load_state_dict(state,strict=True);model=model.to(device).eval()
                output=ColorizationPipeline(model,device=torch.device(device)).process(bgr)
                image=Image.fromarray(output[:,:,::-1]);image=ImageEnhance.Color(image).enhance(float(options.get('saturation',1)))
                del model
            elif operation=='upscale':
                from basicsr.archs.rrdbnet_arch import RRDBNet
                from realesrgan import RealESRGANer
                scale=int(options.get('scale',2))
                limit=16_000_000/(scale*scale)
                if image.width*image.height>limit:
                    factor=(limit/(image.width*image.height))**.5
                    image=image.resize((max(1,int(image.width*factor)),max(1,int(image.height*factor))),Image.Resampling.LANCZOS)
                    bgr=np.array(image)[:,:,::-1].copy()
                net=RRDBNet(num_in_ch=3,num_out_ch=3,num_feat=64,num_block=23,num_grow_ch=32,scale=scale)
                restorer=RealESRGANer(scale=scale,model_path=str(models/f'RealESRGAN_x{scale}plus.pth'),model=net,
                    tile=int(options.get('tile',256)),tile_pad=10,pre_pad=0,half=device.startswith('cuda'),device=torch.device(device))
                result,_=restorer.enhance(bgr,outscale=scale)
                image=Image.fromarray(result[:,:,::-1]);del restorer,net
            elif operation=='faces':
                from gfpgan import GFPGANer
                helper=GFPGANer(model_path=str(models/'GFPGANv1.4.pth'),upscale=1,arch='clean',device=torch.device(device))
                _,faces,result=helper.enhance(bgr,has_aligned=False,only_center_face=False,paste_back=True)
                if result is not None:image=Image.blend(image,Image.fromarray(result[:,:,::-1]),float(options.get('face_strength',.35)))
                del helper
            elif operation=='inpaint':
                # LaMa uses FFT; CPU is the stable fallback on Apple Silicon.
                lama_device='cpu' if device=='mps' else device
                model=torch.jit.load(str(models/'big-lama.pt'),map_location=lama_device).eval()
                mask=Image.open(options['mask']).convert('L').resize(image.size,Image.Resampling.NEAREST)
                if not mask.getbbox():raise ValueError('Отметьте кистью повреждённую область')
                x0,y0,x1,y1=mask.getbbox()
                box=(max(0,x0-96),max(0,y0-96),min(image.width,x1+96),min(image.height,y1+96))
                patch=image.crop(box);patch_mask=mask.crop(box);original_size=patch.size
                factor=min(1,1024/max(patch.size))
                if factor<1:
                    size=(max(1,int(patch.width*factor)),max(1,int(patch.height*factor)))
                    patch=patch.resize(size,Image.Resampling.LANCZOS);patch_mask=patch_mask.resize(size,Image.Resampling.NEAREST)
                rgb=np.array(patch).astype('float32')/255
                mask_array=(np.array(patch_mask)>0).astype('float32')
                tensor=torch.from_numpy(rgb.transpose(2,0,1)).unsqueeze(0).to(lama_device)
                mask_tensor=torch.from_numpy(mask_array).unsqueeze(0).unsqueeze(0).to(lama_device)
                h,w=patch.height,patch.width;pad=(0,(-w)%8,0,(-h)%8)
                tensor=torch.nn.functional.pad(tensor,pad,mode='replicate');mask_tensor=torch.nn.functional.pad(mask_tensor,pad,mode='replicate')
                with torch.inference_mode():result=model(tensor,mask_tensor)
                result=result[0,:,:h,:w].cpu().numpy().transpose(1,2,0)
                generated=Image.fromarray((result.clip(0,1)*255).round().astype('uint8')).resize(original_size,Image.Resampling.LANCZOS)
                patch=Image.composite(generated,image.crop(box),mask.crop(box))
                image.paste(patch,box[:2])
                del model
            if device.startswith('cuda'):torch.cuda.empty_cache()
        path=out/f'{index:02d}-{operation}.png';save_rgb(image,path)
        results.append({'file':str(path),'label':LABELS[operation]})
        progress(int((index+1)/len(config['operations'])*100),LABELS[operation]+' — готово')
    (out/'result.json').write_text(json.dumps(results),encoding='utf-8')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--probe',action='store_true');parser.add_argument('--request')
    parser.add_argument('--export-source');parser.add_argument('--export-target');parser.add_argument('--dpi',type=int,default=300)
    args=parser.parse_args()
    if args.probe:probe();return
    if args.export_source:
        save_rgb(read_rgb(args.export_source),Path(args.export_target),args.dpi);return
    config=json.loads(Path(args.request).read_text(encoding='utf-8'))
    try:process(config)
    except Exception as error:
        detail=str(error)
        if isinstance(error,ModuleNotFoundError):detail='AI-зависимости отсутствуют. Запустите setup-ai-windows.bat или setup-ai-mac.sh'
        elif 'out of memory' in detail.lower():detail='Недостаточно памяти. Уменьшите размер фрагмента или выберите CPU'
        Path(config['output'],'error.txt').write_text(detail,encoding='utf-8')
        raise
if __name__=='__main__':main()

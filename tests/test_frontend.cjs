// Test actual image-processing functions using a small in-memory canvas transport.
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
class Canvas {
  getContext(){return {drawImage:(source)=>{this.pixels=new Uint8ClampedArray(this.width*this.height*4);const sw=source.naturalWidth||source.width,sh=source.naturalHeight||source.height;for(let y=0;y<this.height;y++)for(let x=0;x<this.width;x++){const i=(y*this.width+x)*4,j=(Math.floor(y*sh/this.height)*sw+Math.floor(x*sw/this.width))*4;this.pixels.set(source.pixels.slice(j,j+4),i);}},getImageData:()=>({data:new Uint8ClampedArray(this.pixels)}),putImageData:image=>this.pixels=image.data};}
  toDataURL(){return JSON.stringify({width:this.width,height:this.height,pixels:[...this.pixels]});}
}
const context=vm.createContext({document:{createElement:()=>new Canvas()},Uint8ClampedArray,Uint32Array,console});
const code=readFileSync('static/app.js','utf8');vm.runInContext(code.slice(0,code.lastIndexOf("$('#file-input').onchange")),context);
function image(w,h,values){const pixels=new Uint8ClampedArray(w*h*4);for(let i=0;i<w*h;i++)pixels.set([values[i],values[i],values[i],255],i*4);return {naturalWidth:w,naturalHeight:h,pixels};}
async function runProcessing(source,opts,contrast=1){context.source=source;context.opts=opts;context.contrast=contrast;return JSON.parse(await vm.runInContext('loadImage=async()=>source; improve("test",opts,contrast)',context));}
(async()=>{
 const source=image(3,3,[100,100,100,100,110,100,100,100,100]);const original=[...source.pixels];
 const unchanged=await runProcessing(source,{});assert.deepEqual(unchanged.pixels,original);console.log('PASS: disabled processing preserves all pixels');
 const denoised=await runProcessing(source,{denoise:true});assert(denoised.pixels[16]<110&&denoised.pixels[16]>100);assert.equal(denoised.pixels[19],255);console.log('PASS: noise is reduced and alpha preserved');
 assert.deepEqual([...source.pixels],original);console.log('PASS: source pixels never change');
 const tonal=await runProcessing(image(2,1,[80,160]),{contrast:true});assert(tonal.pixels[4]-tonal.pixels[0]>80);console.log('PASS: automatic contrast expands the tonal range');
 const doubled=await runProcessing(source,{upscale:true});assert.equal(doubled.width,6);assert.equal(doubled.height,6);console.log('PASS: enlargement doubles both dimensions');
 const escaped=vm.runInContext('esc("<img src=x onerror=alert(1)>")',context);assert(!escaped.includes('<'));console.log('PASS: uploaded filenames are escaped in HTML');
})().catch(e=>{console.error(e);process.exitCode=1;});

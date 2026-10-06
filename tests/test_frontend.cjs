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
 context.fixture={photos:[{id:'photo1',name:'<script>bad()</script>.png',original:'/photos/one.png',result:null,client:'Анна',created:'2026-10-01 12:00:00',status:'new',price:1200,version_count:0}],jobs:[{id:'job1',photo_id:'photo1',kind:'process',preset:'color',status:'running',progress:25,stage:'Раскраска',device:'cuda:0',attempts:1,photo_name:'Archive.png'}],orders:[{id:'order1',name:'Архив',client:'Анна',total:5000,deposit:1000,remaining:4000,due:'',notes:'',status:'working',photos:[]}],models:{models:[{id:'ddcolor',name:'DDColor',purpose:'Раскраска',size:'912 МБ',license:'Apache-2.0',link:'https://github.com/piddnad/DDColor',downloaded:true}],runtime:{installed:true,pillow:true,devices:['cpu','cuda:0'],gpus:[{id:'cuda:0',name:'GPU',total_mb:12288}]}}};
 vm.runInContext('photos=fixture.photos;jobs=fixture.jobs;ordersData=fixture.orders;modelData=fixture.models;settings={devices:["auto"],color_model:"ddcolor",tile:256};',context);
 for(const name of ['studio','library','orders','queuePage','tools','settingsPage']){const html=vm.runInContext(name+'()',context);assert(html.length>100);assert(!html.includes('<script>bad()'));}
 assert(vm.runInContext('library().includes("data-select")',context));
 assert(vm.runInContext('queuePage()',context).includes('value="25"'));
 console.log('PASS: all six workspace pages render with orders, GPU jobs and escaped filenames');


 const nodes={};const node=key=>nodes[key]??=( {value:'',innerHTML:'',textContent:'',open:false,querySelectorAll:()=>[],addEventListener(){},removeEventListener(){},showModal(){this.open=true;},close(){this.open=false;this.closed=(this.closed||0)+1;},classList:{add(){},remove(){}},click(){}} );
 const ui=vm.createContext({document:{querySelector:node,querySelectorAll:()=>[]},URL:{createObjectURL:()=> 'blob:preview',revokeObjectURL(){}},console});
 vm.runInContext(code.slice(0,code.lastIndexOf("$('#file-input').onchange")),ui);
 const calls=[];let secondAttempts=0;
 ui.apiStub=async(path,payload)=>{calls.push({path,payload});if(path.startsWith('/api/orders'))return {id:'new-order',client:'Анна'};if(payload.name==='second.png'&&++secondAttempts===1)throw Error('Временный сбой');return {id:payload.name==='first.png'?'first-id':'second-id'};};
 vm.runInContext('api=apiStub;readFile=async file=>file.name;loadImage=async()=>({naturalWidth:100,naturalHeight:100});refresh=async()=>{};toast=()=>{};openOrder();',ui);
 assert(node('#modal').innerHTML.includes('id="order-files"'));assert(node('#modal').innerHTML.includes('multiple'));assert(node('#modal').innerHTML.includes('id="order-drop"'));
 node('#order-name').value='Заказ';node('#order-client').value='Анна';node('#order-total').value='1200';node('#order-deposit').value='0';node('#order-status').value='new';
 node('#order-files').onchange({target:{files:[{name:'first.png',type:'image/png',size:100},{name:'second.png',type:'image/png',size:100}],value:''}});
 await node('#order-form').onsubmit({preventDefault(){}});
 assert(node('#order-feedback').textContent.includes('Не удалось загрузить: 1'));assert.equal(node('#modal').closed,undefined);
 await node('#order-form').onsubmit({preventDefault(){}});
 assert.equal(calls.filter(c=>c.path==='/api/orders').length,1);
 assert.equal(calls.filter(c=>c.path==='/api/photos'&&c.payload.name==='first.png').length,1);
 assert.equal(calls.filter(c=>c.path==='/api/photos'&&c.payload.name==='second.png').length,2);
 const saved=calls.find(c=>c.path==='/api/orders/new-order');assert.deepEqual([...saved.payload.photo_ids],['first-id']);
 assert(calls.filter(c=>c.path==='/api/photos').every(c=>c.payload.order_id==='new-order'&&c.payload.client==='Анна'));
 assert.equal(node('#modal').closed,1);
 console.log('PASS: order form uploads photos immediately and retries failures without duplicate orders or photos');
 ui.badFile={name:'huge.png',type:'image/png',size:26*1024*1024};assert.throws(()=>vm.runInContext('validateOrderFile(badFile)',ui),/25 МБ/);
 ui.badFile={name:'document.pdf',type:'application/pdf',size:100};assert.throws(()=>vm.runInContext('validateOrderFile(badFile)',ui),/JPG/);
 console.log('PASS: order intake rejects unsupported and oversized files');
 context.fixture.jobs=[{id:'download-failed',kind:'download',preset:'ddcolor',status:'failed',error:'HTTPS: <invalid certificate>'}];vm.runInContext('jobs=fixture.jobs',context);
 const failedCard=vm.runInContext('tools()',context);assert(failedCard.includes('&lt;invalid certificate&gt;'));assert(!failedCard.includes('<invalid certificate>'));
 console.log('PASS: model download errors are visible and escaped on the model card');

})().catch(e=>{console.error(e);process.exitCode=1;});

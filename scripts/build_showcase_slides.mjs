/** Editable showcase deck. Uses the bundled presentation runtime and existing demo captures. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(ROOT, 'output', 'slides');
const BUILD = path.join(OUT, '.build');
const RUNTIME = process.env.CODEX_MEDIA_RUNTIME;
const SKILL = process.env.CODEX_PRESENTATIONS_SKILL;
if (!RUNTIME || !SKILL) {
  throw new Error('Optional authoring tool: set CODEX_MEDIA_RUNTIME and CODEX_PRESENTATIONS_SKILL to your installed Codex runtime and presentation skill paths. The app and exported media do not require these tools.');
}
process.env.RUNTIME_NODE_MODULES = path.join(RUNTIME, 'node/node_modules');
process.env.RUNTIME_NODE = path.join(RUNTIME, 'node/bin/node.exe');
process.env.RUNTIME_PYTHON = path.join(RUNTIME, 'python/python.exe');
const { Presentation, PresentationFile, FileBlob } = await import(pathToFileURL(path.join(RUNTIME, 'node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs')).href);
const { finalizePresentation } = await import(pathToFileURL(path.join(SKILL, 'container_tools/artifact_tool_utils.mjs')).href);
await fs.mkdir(BUILD, { recursive: true });
const W=1280,H=720,BG='#0B202B',MINT='#76E1BD',WHITE='#F5F8F5',MUTED='#A7BFC4',FONT='Segoe UI';
const ppt = Presentation.create({slideSize:{width:W,height:H}});
const titles=[];

function text(s,value,x,y,w,h,size=26,color=WHITE,bold=false){
  const shape=s.shapes.add({geometry:'textbox',name:value.slice(0,60),position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
  shape.text=value;
  shape.text.style={typeface:FONT,fontSize:size,color,bold,autoFit:'none'};
  return shape;
}
function slide(title,notes){
  const s=ppt.slides.add();s.background.fill=BG;titles.push(title);
  text(s,title,48,42,1168,82,46,WHITE,true);
  text(s,String(titles.length).padStart(2,'0')+' / 08',1160,672,80,25,18,MUTED);
  s.speakerNotes.textFrame.setText(notes);
  return s;
}
async function screenshot(s,name,x=48,y=145,w=868,h=490){
  const blob=await fs.readFile(path.join(ROOT,'output/demo/captures',name));
  s.images.add({blob,contentType:'image/jpeg',alt:'Authentic Streamlit classifier screen: '+name,fit:'contain',position:{left:x,top:y,width:w,height:h}});
}
function lines(s,rows,x=956,y=170,w=270,size=25,gap=86){
  rows.forEach((row,i)=>text(s,row,x,y+i*gap,w,gap-8,size));
}

// Cover follows the video's navy, mint, Segoe UI direction with editable text.
{
  const s=ppt.slides.add();s.background.fill=BG;titles.push('Management Agent');
  text(s,'MANAGEMENT AGENT',64,80,1100,44,25,MINT,true);
  text(s,'Resident request\nclassifier',60,205,1156,180,76,WHITE,true);
  text(s,'Condominium operations prototype',64,431,1100,55,32,MUTED);
  text(s,'Working local website with an offline classifier and Ollama inference',64,570,1080,72,27);
  s.speakerNotes.textFrame.setText('Slide version of the existing edited video showcase. Visual direction: output/demo/stills and scripts/render_demo.py. Updated local Ollama evidence comes from reports/ollama-verification.json. The application classifies requests and suggests routing. Ticket creation and autonomous agents belong to future phases.');
}
{
  const s=slide('Resident intake','Source: output/demo/captures/01-intake.jpg. Authentic app capture with a synthetic message. The default offline baseline uses the project\'s train.csv and requires no API key.');
  await screenshot(s,'01-intake.jpg');
  lines(s,['A resident writes a message','Staff can add a location','10 fixed request categories'],952,180,270,27,128);
  text(s,'Actual app screen with a synthetic resident message',48,644,1000,30,20,MUTED);
}
{
  const s=slide('Classifier workflow','Sources: condo_classifier/service.py, schema.py, backends.py and README.md. The website calls the existing project Classifier. LangGraph orders the safety screen, backend classification and review policy. Emergency rules bypass model inference. Model outputs undergo schema and evidence validation. No work tickets or emergency dispatch occur.');
  const steps=[
    ['01','Resident message','The website passes the request to this project\'s Classifier.'],
    ['02','Safety screen','Explicit urgent reports bypass the model and require staff triage.'],
    ['03','Selected classifier','Offline TF-IDF baseline or local Gemma through Ollama.'],
    ['04','Validation and review','Staff inspect the category, priority, evidence and suggested team.'],
  ];
  steps.forEach(([n,label,body],i)=>{
    let y=150+i*116;
    text(s,n,52,y,64,48,33,MINT,true);
    text(s,label,144,y,1030,43,32,WHITE,true);
    text(s,body,144,y+47,1040,54,25,MUTED);
  });
}
{
  const s=slide('Structured classification','Source: output/demo/captures/02-maintenance.jpg. The pictured request demonstrates the offline classifier, with maintenance category, normal priority and suggested maintenance team. A model score does not represent validated real-world accuracy.');
  await screenshot(s,'02-maintenance.jpg');
  lines(s,['Category: Maintenance','Priority: Normal','Suggested team: Maintenance'],952,190,275,27,113);
  text(s,'Uncertain requests remain visible for staff review',48,644,1040,30,20,MUTED);
}
{
  const s=slide('Urgent reports and human review','Source: output/demo/captures/03-emergency.jpg. Synthetic child trapped in lift example. Safety rules identify selected explicit English emergency phrases. All Ollama classifications require human confirmation because model confidence is uncalibrated. The app flags urgent reports but does not dispatch assistance.');
  await screenshot(s,'03-emergency.jpg');
  lines(s,['Emergency rules run first','Staff confirm every Gemma result','The app flags urgency. People act.'],952,180,274,27,126);
  text(s,'No emergency dispatch or automatic ticket creation',48,644,1000,30,20,MUTED);
}
{
  const s=slide('Batch review','Source: output/demo/captures/04-batch.jpg. Batch supports up to 100 rows and a 2 MB CSV. The text column is required. Invalid inputs and provider errors receive row-level status. Exports include classifications for review, with spreadsheet formula protection.');
  await screenshot(s,'04-batch.jpg');
  lines(s,['Upload a CSV inbox','Inspect each request and its status','Export the classification results'],952,180,276,27,128);
  text(s,'Up to 100 rows per CSV upload',48,644,1000,30,20,MUTED);
}
{
  const s=slide('Local evaluation results','Measured on 3 October 2026 in Singapore time. Sources: reports/ollama-verification.json, reports/baseline-evaluation.json and README.md. Comparison excludes five emergency cases handled by rules. Both classifiers ran the same 50 ordinary synthetic requests. Gemma produced 48 valid responses, of which 42 had correct categories. Two invalid outputs went to review. Warm valid response mean 1123.69375 ms, mean all attempts 1316.566 ms, cold classification 86598.8 ms. Hardware RTX 3080 with 10 GB VRAM. Gemma3:4b 4.3B Q4_K_M. Synthetic development evidence is not independent real resident validation.');
  text(s,'Same 50 ordinary synthetic requests. Five additional emergencies used rules.',48,139,1170,66,26,MUTED);
  const vals=[['Outcome','Offline baseline','Gemma 3'],['Valid, correct category','48 / 50','42 / 50'],['Rejected model outputs','0','2'],['Default website engine','Yes','Optional']];
  const t=s.tables.add({rows:4,columns:3,left:48,top:232,width:1168,height:236,columnWidths:[572,298,298],values:vals});
  t.borders.assign({fill:'#31545F',width:1,style:'solid'});
  for(let r=0;r<4;r++) for(let c=0;c<3;c++){
    const cell=t.getCell(r,c);cell.fill=r===0?'#163940':BG;
    cell.text.style={typeface:FONT,fontSize:27,color:r===0?MINT:WHITE,bold:r===0};
  }
  text(s,'gemma3:4b    4.3B parameters    Q4_K_M    RTX 3080',48,497,1160,43,27,MINT,true);
  text(s,'1.12 s average valid warm response    1.32 s including rejected attempts',48,552,1170,43,26);
  text(s,'First cold response: 86.6 s. Measurements: 3 October 2026.',48,607,1170,34,22,MUTED);
  text(s,'Synthetic development checks. Real resident accuracy remains unverified.',48,654,1080,28,19,MUTED);
}
{
  const s=slide('Local startup','Startup supplied by project launcher start_project.cmd and scripts/start_project.ps1. From C:\\Users\\Skith\\Documents\\MSG, running start_project.cmd starts or reuses local Ollama at 127.0.0.1:11434 and Streamlit at localhost:8501, then opens the website. The app defaults to Offline baseline. Select Ollama in its sidebar to use gemma3:4b. The offline-only option is start_project.cmd -SkipOllama. Existing standalone Ollama installation is scoped to this workstation. For a clean installation follow README.md.');
  text(s,'1',48,158,60,50,36,MINT,true);
  text(s,'Open the project folder',130,158,1080,46,32,WHITE,true);
  text(s,'C:\\Users\\Skith\\Documents\\MSG',130,211,1050,43,27,MUTED);
  text(s,'2',48,290,60,50,36,MINT,true);
  text(s,'Run start_project.cmd',130,290,1060,46,32,WHITE,true);
  text(s,'The launcher starts Ollama and the website.',130,343,1070,48,27,MUTED);
  text(s,'3',48,422,60,50,36,MINT,true);
  text(s,'Open http://localhost:8501/',130,422,1060,47,32,WHITE,true);
  text(s,'Choose Offline baseline or Ollama in the sidebar.',130,475,1070,48,27,MUTED);
  text(s,'Offline only: start_project.cmd -SkipOllama',130,579,1070,43,25,MINT);
}

const draft=path.join(BUILD,'showcase-candidate.pptx');
const final=path.join(OUT,'Management_Agent_Classifier_Showcase.pptx');
const validatedDir=path.join(BUILD,'validated-'+Date.now());
await fs.mkdir(validatedDir,{recursive:true});
const validatedFinal=path.join(validatedDir,path.basename(final));
await (await PresentationFile.exportPptx(ppt)).save(draft);
// Finalization creates a new result and will not overwrite an earlier deck.
await finalizePresentation({workspaceDir:ROOT,candidatePath:draft,finalPath:validatedFinal,pythonExecutable:path.join(RUNTIME,'python/python.exe'),integrityValidatorPath:path.join(SKILL,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(SKILL,'container_tools/inspect_presentation_layout_geometry.py'),layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-heading-fit','--require-native-table-slide','7'],explicitTotalSlideCount:8,requiredNativeTableOwnerSlides:[7],fontPolicy:{basis:'design',families:[FONT]},verifyArtifactToolImport:true,receiptPath:path.join(BUILD,path.basename(validatedDir)+'.validation.json')});
await fs.copyFile(validatedFinal,final);
const renderedPresentation=await PresentationFile.importPptx(await FileBlob.load(final));

for(let i=0;i<8;i++){
  const s=renderedPresentation.slides.items[i];
  const rendered=await renderedPresentation.export({slide:s,format:'png',scale:1.5});
  await fs.writeFile(path.join(OUT,`slide-${String(i+1).padStart(2,'0')}.png`),new Uint8Array(await rendered.arrayBuffer()));
  const layout=await s.export({format:'layout'});
  await fs.writeFile(path.join(BUILD,`slide-${i+1}.layout.json`),await layout.text());
}
const montage=await renderedPresentation.export({format:'png',montage:true,scale:1.875});
await fs.writeFile(path.join(OUT,'contact-sheet.png'),new Uint8Array(await montage.arrayBuffer()));
await fs.writeFile(path.join(OUT,'slides.json'),JSON.stringify({title:'Management Agent classifier showcase',slide_count:8,titles,measurements_date:'2026-10-03',source_video:'../demo/Management_Agent_Classifier_Demo.mp4',notes:'Slide text and comparison table are editable in PPTX. Images show authentic synthetic demonstration screens. The video predates the live Ollama evaluation.'},null,2));
const html=`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Management Agent showcase</title><style>body{margin:0;background:${BG};color:${WHITE};font:18px 'Segoe UI',sans-serif}main{max-width:1280px;margin:auto;padding:24px}header{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin-bottom:20px}h1{font-size:25px;margin:0 auto 0 0}a{color:${MINT}}button{font:inherit;padding:9px 18px;background:#163940;border:1px solid #76e1bd;color:${WHITE};cursor:pointer}button:focus-visible,a:focus-visible{outline:3px solid white;outline-offset:4px}img{width:100%;height:auto;display:block}p{color:${MUTED};line-height:1.5}button:disabled{opacity:.5;cursor:default}</style></head><body><main><header><h1>Management Agent classifier</h1><a href="Management_Agent_Classifier_Showcase.pptx">PowerPoint</a><a href="Management_Agent_Classifier_Showcase.pdf">PDF</a><button id="prev">Previous</button><span id="counter"></span><button id="next">Next</button></header><img id="slide" alt=""><p>Use Previous and Next or the left and right arrow keys. The deck includes updated local Ollama results measured on 3 October 2026.</p></main><script>const titles=${JSON.stringify(titles)};let current=0;function show(){const img=document.getElementById('slide');img.src='slide-'+String(current+1).padStart(2,'0')+'.png';img.alt=titles[current];document.getElementById('counter').textContent=(current+1)+' / '+titles.length;document.getElementById('prev').disabled=current===0;document.getElementById('next').disabled=current===titles.length-1;}document.getElementById('prev').onclick=()=>{if(current>0){current--;show();}};document.getElementById('next').onclick=()=>{if(current<titles.length-1){current++;show();}};document.addEventListener('keydown',e=>{if(e.key==='ArrowLeft')document.getElementById('prev').click();if(e.key==='ArrowRight')document.getElementById('next').click();});show();</script></body></html>`;
await fs.writeFile(path.join(OUT,'index.html'),html);
// PDF is a faithful visual companion. Editable objects are retained in the PPTX.
const python=`from pathlib import Path

from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

p = Path(r"${OUT}")
c = canvas.Canvas(str(p / "Management_Agent_Classifier_Showcase.pdf"), pagesize=(960, 540))
c.setTitle("Management Agent Classifier Showcase")
for i in range(1, 9):
    c.drawImage(ImageReader(str(p / f"slide-{i:02}.png")), 0, 0, width=960, height=540)
    c.showPage()
c.save()
print("PDF created: 8 pages")
`;
await fs.writeFile(path.join(BUILD,'make_pdf.py'),python);
const p=spawnSync(path.join(RUNTIME,'python/python.exe'),[path.join(BUILD,'make_pdf.py')],{encoding:'utf8'});
if(p.status!==0)throw new Error(p.stderr);
console.log(p.stdout);console.log(JSON.stringify({pptx:final,slide_count:8,pngs:8,viewer:path.join(OUT,'index.html')}));

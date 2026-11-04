const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repo = path.join(__dirname, '..');
const app = fs.readFileSync(path.join(repo, 'dist', 'app.js'), 'utf8');

test('the open source page presents the public repository and actual release status', () => {
  const events = {};
  const root = {innerHTML:'', addEventListener:(event, handler)=>{events[event]=handler;}};
  const skip = {textContent:''};
  const document = {getElementById:id=>id==='app'?root:null, querySelector:()=>skip, documentElement:{lang:'',dir:''}, addEventListener:()=>{}, title:''};
  const store = {getItem:()=>null,setItem:()=>{}};
  vm.runInNewContext(app, {document, sessionStorage:store, localStorage:store, window:{scrollTo:()=>{}}, structuredClone, setTimeout, clearTimeout});
  events.click({target:{closest:()=>({disabled:false,dataset:{action:'open-source'}})}});
  assert.match(root.innerHTML, /https:\/\/github\.com\/ziiyuu-lgtm\/intelligent-publishing-studio/);
  assert.match(root.innerHTML, /This prototype is open source under the Apache License 2\.0/);
  assert.doesNotMatch(root.innerHTML, /public source repository is not yet available/);
  assert.equal(document.title, 'Open Source | Altavien');
});

test('the initial source release excludes hosting configuration', () => {
  assert.equal(fs.existsSync(path.join(repo, '.openai')), false);
  assert.match(fs.readFileSync(path.join(repo,'.gitignore'),'utf8'), /^\.openai\/$/m);
});

test('the independent preview keeps sample journal navigation and the software review example', () => {
  const events = {};
  const root = {innerHTML:'', addEventListener:(event, handler)=>{events[event]=handler;}};
  const document = {getElementById:id=>id==='app'?root:null, querySelector:()=>({textContent:''}),
    documentElement:{lang:'',dir:''}, addEventListener:()=>{}, title:''};
  const store = {getItem:()=>null,setItem:()=>{}};
  vm.runInNewContext(app, {document, sessionStorage:store, localStorage:store,
    window:{scrollTo:()=>{}}, structuredClone, setTimeout, clearTimeout});
  assert.match(root.innerHTML, /ALTAVIEN/);
  assert.match(root.innerHTML, /SAMPLE A/);
  assert.match(root.innerHTML, /SAMPLE D/);
  events.click({target:{closest:()=>({disabled:false,dataset:{action:'journal',id:'sample_d'}})}});
  assert.equal(document.title, 'SAMPLE D | Altavien');
  assert.match(root.innerHTML, /Artifact review/);
  events.click({target:{closest:()=>({disabled:false,dataset:{action:'start'}})}});
  assert.equal(document.title, 'Submit a Manuscript | Altavien');
  assert.match(root.innerHTML, /value="sample_d" checked/);
});

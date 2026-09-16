import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createBlueprint,createMachine,resolveComponents,validateBlueprint,estimateMachine,planBlueprint} from './planner.mjs';
const catalog=JSON.parse(fs.readFileSync(new URL('./catalog.json',import.meta.url),'utf8'));

test('every catalog preset is a valid export with resolvable dependencies',()=>{
  for(const goal of catalog.goals){
    const doc=createBlueprint(catalog,goal.id);
    assert.deepEqual(validateBlueprint(JSON.parse(JSON.stringify(doc)),catalog),{valid:true,errors:[]});
    assert.equal(planBlueprint(doc,catalog).machines.length,1);
  }
});
test('duplicate identities and unknown executable or secret-bearing fields fail',()=>{
  const doc=createBlueprint(catalog);
  doc.machines.push(structuredClone(doc.machines[0]));
  assert.equal(validateBlueprint(doc,catalog).valid,false);
  for(const [target,key] of [[doc,'token'],[doc.machines[0],'command'],[doc.machines[0].workload,'endpoint']]){
    const fresh=createBlueprint(catalog);
    const object=target===doc?fresh:target===doc.machines[0]?fresh.machines[0]:fresh.machines[0].workload;
    object[key]='untrusted';
    assert.equal(validateBlueprint(fresh,catalog).valid,false);
  }
});
test('numeric boundaries reject NaN, booleans, strings, fractions and negatives',()=>{
  for(const value of [NaN,Infinity,true,'2',-1,1.5,129]){
    const doc=createBlueprint(catalog);doc.machines[0].workload.agents=value;
    assert.equal(validateBlueprint(doc,catalog).valid,false,String(value));
  }
  const doc=createBlueprint(catalog);doc.machines[0].workload.context_tokens=32769;
  assert.equal(validateBlueprint(doc,catalog).valid,false);
});
test('dependencies, role and platform incompatibility cannot be silently exported',()=>{
  assert.deepEqual(resolveComponents(catalog,['training']),['adapters','training']);
  const doc=createBlueprint(catalog,'learning');
  doc.machines[0].components=['router'];
  assert.equal(validateBlueprint(doc,catalog).valid,false);
  doc.machines[0]=createMachine(catalog,{goal:'learning'});doc.machines[0].profile='tools';
  assert.equal(validateBlueprint(doc,catalog).valid,false);
  doc.machines[0]=createMachine(catalog,{goal:'learning'});doc.machines[0].platform='linux-x64';
  assert.equal(validateBlueprint(doc,catalog).valid,false);
});
test('Windows is explicitly unsupported even when the planning document is valid',()=>{
  const doc=createBlueprint(catalog);doc.machines[0].platform='windows';
  const result=planBlueprint(doc,catalog);
  assert.equal(result.valid,true);
  assert.match(result.machines[0].warnings.join(' '),/does not support Windows/);
});
test('larger workloads and data never lower a per-machine recommendation',()=>{
  const original=createMachine(catalog);original.workload.agents=4;
  const baseline=estimateMachine(original,catalog);
  for(const key of ['agents','browsers','builds','memory_millions','project_gb','context_tokens']){
    const machine=structuredClone(original);
    machine.workload[key]=key==='context_tokens'?32768:key==='project_gb'?500:key==='memory_millions'?10:16;
    const next=estimateMachine(machine,catalog);
    for(const resource of ['ram_gb','cpu_cores','free_disk_gb'])assert.ok(next[resource]>=baseline[resource]);
    assert.ok(['ram_gb','cpu_cores','free_disk_gb'].some(resource=>next[resource]>baseline[resource]),key);
  }
});
test('mixed nodes are computed independently, not total divided by node count',()=>{
  const doc=createBlueprint(catalog);const first=estimateMachine(doc.machines[0],catalog);
  const worker=createMachine(catalog,{id:'build-node',goal:'custom'});worker.profile='tools';worker.workload.builds=5;
  doc.machines.push(worker);
  const result=planBlueprint(doc,catalog);
  assert.deepEqual(result.machines[0].estimate,first);
  assert.equal(result.totals.ram_gb,result.machines.reduce((n,m)=>n+m.estimate.ram_gb,0));
  assert.notEqual(result.machines[0].estimate.cpu_cores,result.machines[1].estimate.cpu_cores);
});
test('research and separately hosted integration limits remain visible',()=>{
  const doc=createBlueprint(catalog,'learning');const plan=planBlueprint(doc,catalog);
  assert.ok(plan.machines[0].estimate.ram_gb>=64);
  assert.match(plan.machines[0].warnings.join(' '),/Training is not enabled/);
  assert.match(plan.machines[0].warnings.join(' '),/External-service compute/);
  assert.match(plan.machines[0].setup.join(' '),/borg adapters prepare/);
});
test('invalid plans return no estimates and component dependency cycles fail',()=>{
  assert.equal(planBlueprint(null,catalog).machines.length,0);
  const cycle=structuredClone(catalog);cycle.items.find(x=>x.id==='codex').requires=['router'];
  assert.throws(()=>resolveComponents(cycle,['router']),/Cyclic/);
});
test('catalog sources and IDs are unique, public and dependency complete',()=>{
  assert.equal(new Set(catalog.items.map(x=>x.id)).size,catalog.items.length);
  for(const item of catalog.items){
    assert.ok(item.setup.length>0);assert.ok(item.license.length>0);
    for(const dep of item.requires)assert.ok(catalog.items.some(row=>row.id===dep&&row.type==='component'));
    for(const link of [item.source,item.docs])assert.ok(link.startsWith('https://')||(!link.startsWith('/')&&!link.includes('..')));
  }
});

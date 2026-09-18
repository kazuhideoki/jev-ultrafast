"""Offline shared-validator contracts; Node evaluates the shipped JavaScript."""

import json
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[1] / "jev_ultrafast" / "actionability.js"

FIXTURE = r"""
class Element {
 constructor(tag, attrs={}) {
  this.tagName=tag; this.attrs=attrs; this.isConnected=true; this.disabled=false;
  this.visible=true; this.parentElement=null; this.labels=[]; this.innerText='';
  this.rect={x:30,y:30,width:180,height:30}; this.style={cursor:'default'};
 }
 getAttribute(k){return this.attrs[k]??null}
 hasAttribute(k){return k in this.attrs}
 matches(){return this.disabled}
 closest(selector){
  for(let e=this;e;e=e.parentElement){
   if(selector.includes('[inert]') && e.hasAttribute('inert'))return e;
   if(selector.includes('aria-disabled') && e.attrs['aria-disabled']==='true')return e;
   if(selector.includes('aria-hidden') && e.attrs['aria-hidden']==='true')return e;
  }
  return null;
 }
 checkVisibility(){return this.visible}
 getBoundingClientRect(){return this.rect}
 getClientRects(){return [this.rect]}
 querySelectorAll(){return []}
 contains(n){for(;n;n=n.parentElement)if(n===this)return true;return false}
}
const root=new Element('SPAN');
const select=new Element('SELECT',{'aria-label':'Category'});
select.parentElement=root; select.selectedOptions=[{label:'All'}];
const surface=new Element('SPAN',{'tabindex':'-1','aria-hidden':'true'});
surface.parentElement=root;surface.innerText='Category All';surface.style.cursor='pointer';
let hit=surface;
globalThis.innerWidth=1000;globalThis.innerHeight=800;
globalThis.document={elementFromPoint:()=>hit};
globalThis.getComputedStyle=e=>e.style;
globalThis.window={__jevFast:{nodes:new Map([[1,select]])}};
"""


@pytest.mark.parametrize(("mutation", "surface_expected", "reason"), [
    ("", True, None),
    ("surface.innerText='Different control'", False, "surface_changed"),
    ("hit=new Element('DIV')", False, "surface_changed"),
    ("select.disabled=true", False, "surface_changed"),
    ("surface.attrs.inert=''", False, "target_disabled"),
    ("delete surface.attrs.tabindex", False, "surface_changed"),
    ("surface.visible=false", False, "surface_changed"),
    ("surface.rect.width=1000", False, "surface_changed"),
])
def test_surface_requires_observed_relationship(mutation, surface_expected, reason):
    source = FIXTURE + "\nconst {surfaceFor,resolveTarget}=" + HELPER.read_text() + ";\n" + mutation + ";\n"
    source += "console.log(JSON.stringify({surface:surfaceFor(select)===surface,"
    source += "result:resolveTarget(surface,{kind:'click',proxy_for:1}),native:resolveTarget(select)}));"
    output = subprocess.run(["node", "-e", source], capture_output=True, text=True, check=True)
    result = json.loads(output.stdout)
    assert result["surface"] is surface_expected
    assert result["result"].get("reason") == reason
    # Even a verified surface is a separate CLICK target; never allow the covered native SELECT.
    assert result["native"].get("reason") in {"target_covered", "target_disabled"}

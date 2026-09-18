// Shared read-only validation for observation and execution. No site-specific selectors.
(() => {

const pointFor=e=>{
 const rects=[e.getBoundingClientRect(),...e.getClientRects(),
  ...[...e.querySelectorAll('*')].slice(0,100).flatMap(n=>[...n.getClientRects()])];
 const points=rects.map(r=>({x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height}))
  .filter(p=>p.w>0 && p.h>0 && p.x>=0 && p.y>=0 && p.x<innerWidth && p.y<innerHeight);
 if(!points.length)return {reason:'target_outside_viewport'};
 return points.find(p=>e.contains(document.elementFromPoint(p.x,p.y))) || {reason:'target_covered'};
};
const surfaceFor=e=>{
 if(e?.tagName!=='SELECT' || !e.isConnected || e.matches(':disabled') ||
   e.closest('[aria-disabled="true"],[inert],[aria-hidden="true"]') ||
   !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) || !pointFor(e).reason)return null;
 const parent=e.parentElement;
 if(!parent || ['BODY','HTML'].includes(parent.tagName))return null;
 const r=e.getBoundingClientRect();
 let surface=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
 while(surface?.parentElement && surface.parentElement!==parent)surface=surface.parentElement;
 if(!surface || surface===e || surface.parentElement!==parent ||
    !surface.hasAttribute('tabindex') || getComputedStyle(surface).cursor!=='pointer' ||
    surface.closest('[inert],[aria-disabled="true"]') || surface.matches(':disabled') ||
    !surface.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return null;
 const label=(e.getAttribute('aria-label') || [...e.labels].map(l=>l.innerText).join(' ')).trim();
 const selected=e.selectedOptions[0]?.label.trim(), text=surface.innerText.trim();
 const sr=surface.getBoundingClientRect();
 if(!label || !selected || !text.includes(label) || !text.includes(selected) ||
    sr.width>Math.max(300,r.width*4) || sr.height>Math.max(100,r.height*4))return null;
 return surface;
};
const resolveTarget=(e,action={})=>{
 if(!e?.isConnected)return {reason:'target_missing'};
 if(e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]'))return {reason:'target_disabled'};
 if(action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true'))
  return {reason:'target_readonly'};
 if(action.proxy_for!==undefined){
  if(surfaceFor(window.__jevFast.nodes.get(action.proxy_for))!==e)return {reason:'surface_changed'};
 }else if(e.closest('[aria-hidden="true"]'))return {reason:'target_hidden'};
 if(!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return {reason:'target_hidden'};
 return pointFor(e);
};

return {surfaceFor,resolveTarget};
})()

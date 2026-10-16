// Runs in the visible document. Gray skeleton cards are loading, never emptiness.
export function inspectSearchReadiness() {
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>2&&r.height>2&&r.bottom>0&&r.top<innerHeight&&s.display!=='none'&&s.visibility!=='hidden'&&!e.closest('[aria-hidden="true"]');};
  const input=[...document.querySelectorAll('input[type="search"]')].find(visible);
  const busy=[...document.querySelectorAll('[aria-busy="true"],[role="progressbar"],[data-loading="true"],[class*="skeleton" i]')].some(visible);
  let skeletons=0;
  for(const e of document.querySelectorAll('div')) {
    if(e.children.length||e.textContent.trim()||!visible(e))continue;
    const r=e.getBoundingClientRect();if(r.width<100||r.height<100)continue;
    const s=getComputedStyle(e),rgb=s.backgroundColor.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if(rgb&&Number(rgb[1])>=230&&Number(rgb[1])<255&&Math.abs(Number(rgb[1])-Number(rgb[2]))<6&&Math.abs(Number(rgb[2])-Number(rgb[3]))<6)skeletons++;
  }
  return {loading:document.readyState!=='complete'||busy||skeletons>=3,
    loadingPlaceholderCount:skeletons,searchInputValue:input?.value||''};
}

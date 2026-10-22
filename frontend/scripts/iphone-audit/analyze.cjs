// Digest the per-device overflow-scan JSON into a readable list.
const fs = require('fs')
for (const tag of ['390', '320']) {
  const f = `${tag}-overflow-scan.json`
  if (!fs.existsSync(f)) continue
  const d = JSON.parse(fs.readFileSync(f, 'utf8'))
  console.log(`\n==================== ${tag} ====================`)
  console.log('pageErrors:', d.pageErrors.length, d.pageErrors.slice(0, 8))
  const seen = new Map()
  for (const [state, r] of Object.entries(d.states)) {
    console.log(`\n--- ${state} | media coarse=${r.media.pointerCoarse} hoverNone=${r.media.hoverNone} | html.scrollWidth=${r.document.htmlScrollWidth} body=${r.document.bodyScrollWidth} inner=${r.viewport.innerWidth} | body ${r.bodyFont.fontSize}/${r.bodyFont.lineHeight}`)
    console.log('  fonts:', JSON.stringify(Object.fromEntries(Object.entries(r.fontSizes).filter(([k]) => ['input','select','textarea','button'].includes(k)))))
    console.log('  smallFonts:', r.smallFonts.map((s) => `${s.fontSize} "${s.text.slice(0,25)}" [${s.classes.slice(0,60)}]`).join(' | '))
    if (r.inputsTallerThanRow.length) console.log('  inputsTallerThanRow:', JSON.stringify(r.inputsTallerThanRow))
    for (const fl of r.flagged) {
      const key = fl.kind + '|' + fl.path + '|' + fl.text.slice(0, 30) + '|' + fl.reasons.join(';').replace(/[\d.]+/g, '#')
      if (seen.has(key)) { seen.get(key).states.push(state); continue }
      seen.set(key, { ...fl, states: [state] })
    }
  }
  console.log(`\n### unique flagged for ${tag}: ${seen.size}`)
  const items = [...seen.values()]
  const vis = items.filter((x) => x.reasons.some((r) => r.includes('VISIBLE')))
  const other = items.filter((x) => !x.reasons.some((r) => r.includes('VISIBLE')))
  const show = (x) => {
    console.log(`\n* [${x.kind}] ${x.path}`)
    console.log(`    text: "${x.text}"`)
    console.log(`    reasons: ${x.reasons.join(' ; ')}`)
    console.log(`    font ${x.fontSize}/${x.lineHeight} ws=${x.whiteSpace} ov=${x.overflow} minW=${x.minWidth} disp=${x.display} flex=${x.flex}`)
    console.log(`    rect ${JSON.stringify(x.rect)} parent ${JSON.stringify(x.parentRect)} clippedBy=${x.clippedBy}`)
    console.log(`    classes: ${x.classes.slice(0, 160)}`)
    console.log(`    parent : ${x.parentClasses.slice(0, 160)}`)
    console.log(`    states: ${x.states.join(',')}`)
  }
  console.log('\n######## VISIBLE OVERFLOW candidates ########')
  vis.forEach(show)
  console.log('\n######## clipped / truncated / scroller (not visible) ########')
  other.forEach(show)
}

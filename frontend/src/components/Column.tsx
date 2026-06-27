import type { Entry } from '../api'
import { Card } from './Card'

type Props = {
  name: string
  entries: Entry[]
  sourcesById: Map<number, string>
}

export function Column({ name, entries, sourcesById }: Props) {
  return (
    <section className="flex flex-col h-full overflow-hidden">
      <header className="flex items-center justify-between px-1 pb-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">{name}</h2>
        <span className="text-xs text-slate-500">{entries.length}</span>
      </header>
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {entries.length === 0 ? (
          <p className="text-sm text-slate-500 italic px-1">nothing yet</p>
        ) : (
          entries.map((e) => (
            <Card key={e.id} entry={e} sourceName={sourcesById.get(e.source_id)} />
          ))
        )}
      </div>
    </section>
  )
}
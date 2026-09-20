/**
 * Dependency-free bar chart.
 *
 * Deliberately not a charting library: the dashboard series are small, bounded,
 * already aggregated server-side, and rendering them as labelled divs keeps the
 * page server-rendered, accessible (each bar is a real element with a title and
 * aria-label), and free of a 100 kB client bundle.
 */

export interface BarChartDatum {
  label: string;
  value: number;
}

export default function BarChart({
  data,
  title,
  emptyMessage = 'No data in this window.',
  height = 120,
}: {
  data: BarChartDatum[];
  title: string;
  emptyMessage?: string;
  height?: number;
}) {
  const max = data.reduce((acc, d) => Math.max(acc, d.value), 0);

  return (
    <figure className="flex flex-col">
      <figcaption className="text-xs font-medium uppercase tracking-wider text-slate-500">
        {title}
      </figcaption>
      {data.length === 0 || max === 0 ? (
        <p className="mt-3 text-sm text-slate-500">{emptyMessage}</p>
      ) : (
        <div
          className="mt-3 flex items-end gap-1"
          style={{ height }}
          role="img"
          aria-label={`${title}: ${data.length} buckets, peak ${max}`}
        >
          {data.map((datum) => {
            const ratio = max > 0 ? datum.value / max : 0;
            return (
              <div
                key={datum.label}
                className="flex h-full flex-1 items-end"
                title={`${datum.label}: ${datum.value}`}
              >
                <div
                  className="w-full rounded-t bg-argus-accent/70"
                  style={{ height: `${Math.max(2, ratio * 100)}%` }}
                  aria-label={`${datum.label}: ${datum.value}`}
                />
              </div>
            );
          })}
        </div>
      )}
    </figure>
  );
}

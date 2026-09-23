import Link from 'next/link';

import { NO_HISTORY_NOTE } from '@/lib/intelligence';

import SearchPanel from './SearchPanel';

export const metadata = {
  title: 'Knowledge search',
};

export const dynamic = 'force-dynamic';

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">{title}</h2>
        {subtitle ? <p className="text-xs text-slate-500">{subtitle}</p> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * Grounded knowledge search (§46–§49, §90).
 *
 * The rule that makes this page worth having: **"I have not seen this before" is
 * an acceptable answer.** A search that always produces something is a search
 * that fabricates, so the empty case is stated as a finding and the page says so
 * before the reader asks.
 */
export default async function SearchPage({
  searchParams,
}: {
  searchParams: { project_id?: string };
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Knowledge search</h1>
        <p className="mt-1 text-sm text-slate-400">
          Ask what has happened before in this project. Every answer is assembled
          from stored knowledge, experiences and effectiveness records, and cites
          the rows it used.
        </p>
      </div>

      <Card
        title="Ask a question"
        subtitle="Answers come from this project's stored history only"
      >
        {projectId ? (
          <SearchPanel projectId={projectId} />
        ) : (
          <p className="text-sm text-slate-400">
            Open this page from the{' '}
            <Link href="/intelligence" className="text-argus-accent">
              Learning Center
            </Link>{' '}
            so the project scope travels with the link. Retrieval is scoped, and an
            unscoped query would have to read every project&#39;s history to answer
            anything.
          </p>
        )}
      </Card>

      <Card title="What an empty answer means" subtitle={NO_HISTORY_NOTE}>
        <ul className="space-y-1 text-sm text-slate-400">
          <li>
            · ARGUS will say it found no comparable case rather than describing a
            plausible one.
          </li>
          <li>
            · A cited row that no longer exists is reported as a warning instead of
            being rendered as if it were still there.
          </li>
          <li>
            · Answers never claim a cause, and never promise that a remediation will
            work.
          </li>
        </ul>
      </Card>
    </div>
  );
}

import Link from 'next/link';

import { api, formatDate, type ModelVersionList } from '@/lib/api';
import { calibrationStyle, modelStatusLabel } from '@/lib/reliability';

export const metadata = {
  title: 'Model registry',
};

export const dynamic = 'force-dynamic';

/**
 * The model registry UI (§54).
 *
 * Read-only on purpose: the page shows versions, parameters and evaluation
 * metrics, and offers no deploy/activate control — model lifecycle changes are
 * a backend operation guarded by the data-sufficiency gate, not a button.
 */
export default async function ModelsPage() {
  let models: ModelVersionList | null = null;
  let error: string | null = null;
  try {
    models = await api.listModelVersions(100);
  } catch (cause) {
    error = cause instanceof Error ? cause.message : String(cause);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Model registry</h1>
        <p className="mt-1 text-sm text-slate-400">
          Every forecast references a model version, and every version records its
          algorithm, parameters and evaluation state. The registry is global
          infrastructure — it carries no customer evidence. Phase 8 ships only
          deterministic predictors; ML families are registered but gated behind
          the data-sufficiency check.
        </p>
      </div>

      {error ? (
        <section className="card">
          <p className="text-sm text-argus-error">{error}</p>
        </section>
      ) : null}

      {!error && (!models || models.items.length === 0) ? (
        <section className="card">
          <p className="text-sm text-slate-400">
            No model versions registered yet — generate a forecast pass and the
            predictors self-register.
          </p>
        </section>
      ) : null}

      {models && models.items.length > 0 ? (
        <section className="card">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Model</th>
                  <th className="px-3 py-2">Version</th>
                  <th className="px-3 py-2">Family</th>
                  <th className="px-3 py-2">Algorithm</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Calibration</th>
                  <th className="px-3 py-2">Feature schema</th>
                  <th className="px-3 py-2">Registered</th>
                </tr>
              </thead>
              <tbody>
                {models.items.map((model) => (
                  <tr key={model.id}>
                    <td className="px-3 py-2 text-slate-200">{model.model_name}</td>
                    <td className="px-3 py-2 text-slate-400">{model.version}</td>
                    <td className="px-3 py-2 text-slate-400">{model.model_type}</td>
                    <td className="px-3 py-2 text-slate-400">
                      {model.algorithm ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {modelStatusLabel(model.status)}
                    </td>
                    <td className="px-3 py-2">
                      <span className={`badge ${calibrationStyle(model.calibration_status)}`}>
                        {model.calibration_status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {model.feature_schema_version}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {formatDate(model.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      <div>
        <Link href="/reliability" className="text-xs text-argus-accent hover:underline">
          ← Reliability dashboard
        </Link>
      </div>
    </div>
  );
}

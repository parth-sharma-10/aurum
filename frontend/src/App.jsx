import { useCallback, useEffect, useState } from "react";

const API = import.meta.env.VITE_AURUM_API ?? "http://127.0.0.1:8000";
const POLL_MS = 5000;

const CLASS_COLOR = {
  PCB: "var(--cls-pcb)",
  RAM: "var(--cls-ram)",
  CPU: "var(--cls-cpu)",
  Connector: "var(--cls-connector)",
};

const kg = (grams) => (grams / 1000).toFixed(3);
const pct = (conf) => (conf ? `${(conf * 100).toFixed(1)}%` : "--");
const clock = (iso) => (iso ? iso.replace("T", " ").replace("+00:00", "Z") : "--");

async function getJSON(path) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

/** Weight is either simulated or measured, and the two never merge into one figure. */
function WeightBadge({ weight }) {
  if (!weight) return <span className="badge-c">NO READING</span>;
  return weight.simulated ? (
    <span className="badge-c">SIMULATED</span>
  ) : (
    <span className="badge-b">
      <span className="dot" />
      MEASURED
    </span>
  );
}

/** Where the ledger's mass came from.
 *
 * Absence is not evidence of measurement: with no data, or with a ledger that
 * holds no reading at all, this says so rather than falling through to the
 * green MEASURED badge.
 */
function MassProvenance({ weight }) {
  if (!weight) return <span className="chips">no data</span>;
  if (weight.simulated_grams > 0) {
    return <span className="badge-c">{kg(weight.simulated_grams)} kg SIMULATED</span>;
  }
  if (weight.measured_grams > 0) {
    return (
      <span className="badge-b">
        <span className="dot" />
        MEASURED
      </span>
    );
  }
  return <span className="chips">no mass recorded</span>;
}

/* An estimate and a refusal are different states and must not read alike. The
   estimate always carries its evidence ids, so a reader can trace any figure
   back to the paper it came from via docs/material-reference.md. */
function MaterialEstimate({ estimate }) {
  if (!estimate?.available) {
    return (
      <div className="notice neutral">
        Material estimate: <strong>unavailable</strong>. {estimate?.reason}
      </div>
    );
  }
  const metals = Object.entries(estimate.material_estimate ?? {});
  return (
    <div className="notice">
      <strong>ESTIMATE — not an assay.</strong> Reference composition for the
      detected classes, confidence <strong>{estimate.confidence}</strong>.
      <ul>
        {metals.map(([metal, agg]) => (
          <li key={metal}>
            {metal}: <strong>{agg.typical_g} g</strong> typical
            {agg.max_g != null ? ` (up to ${agg.max_g} g)` : ""} — evidence{" "}
            {agg.evidence.join(", ")}
          </li>
        ))}
      </ul>
      Recovery: <strong>unavailable</strong>. {estimate.recovery?.reason}
    </div>
  );
}

function BatchModal({ record, onClose }) {
  const w = record.weight;
  const fields = [
    ["Batch", record.batch_id],
    ["Model", record.model_version],
    ["Source", record.source],
    ["Started", clock(record.started_at)],
    ["Closed", clock(record.timestamp)],
    ["Frames observed", record.frames_observed],
    ["Objects", record.total_objects],
    ["Mean confidence", pct(record.average_confidence)],
  ];
  return (
    <div className="overlay" onClick={onClose}>
      <div className="glass-panel modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <h2 className="section-title">Batch record</h2>
            <p className="section-note">{record.counting_method}</p>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <div className="field-grid">
          {fields.map(([label, value]) => (
            <div key={label}>
              <div className="field-label">{label}</div>
              <div className="field-value">{value ?? "--"}</div>
            </div>
          ))}
        </div>

        <div className="chips" style={{ marginBottom: 18 }}>
          {Object.entries(record.detections ?? {}).map(([cls, n]) => (
            <span key={cls} className="chip" style={{ "--swatch": CLASS_COLOR[cls] }}>
              {cls} {n}
            </span>
          ))}
        </div>

        {w && (
          <div className={w.simulated ? "notice" : "notice neutral"}>
            <strong>{kg(w.grams)} kg</strong> — {w.source}.{" "}
            {w.warning ?? "Physical reading from the load cell."}
          </div>
        )}

        <MaterialEstimate estimate={record.recovery_estimate} />

        <pre className="record">{JSON.stringify(record, null, 2)}</pre>
      </div>
    </div>
  );
}

export default function App() {
  const [stats, setStats] = useState(null);
  const [batches, setBatches] = useState([]);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);

  const load = useCallback(async () => {
    try {
      const [h, s, b] = await Promise.all([
        getJSON("/health"),
        getJSON("/stats"),
        getJSON("/batches?limit=50"),
      ]);
      setHealth(h);
      setStats(s);
      setBatches(b.batches);
      setError(null);
    } catch (err) {
      setError(`${err.message}. Is the API running at ${API}?`);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  const weight = stats?.total_weight;

  return (
    <div className="shell">
      <header className="glass-panel masthead">
        <div>
          <h1 className="wordmark">AURUM</h1>
          <p className="tagline">E-waste component identification · batch ledger</p>
        </div>
        {error ? (
          <span className="badge-c">API UNREACHABLE</span>
        ) : (
          <span className="badge-b">
            <span className="dot" />
            {health?.status === "ok" ? health.model_version : "MODEL MISSING"}
          </span>
        )}
      </header>

      {error && <div className="glass-panel banner">{error}</div>}

      <div className="metric-row">
        <div className="glass-panel metric">
          <div className="metric-label">Components counted</div>
          <div className="metric-value">{stats?.total_count ?? "--"}</div>
          <div className="metric-foot chips">
            {Object.entries(stats?.component_breakdown ?? {}).map(([cls, n]) => (
              <span key={cls} className="chip" style={{ "--swatch": CLASS_COLOR[cls] }}>
                {cls} {n}
              </span>
            ))}
          </div>
        </div>

        <div className="glass-panel metric">
          <div className="metric-label">Mass on the ledger</div>
          <div className="metric-value">
            {weight ? kg(weight.measured_grams + weight.simulated_grams) : "--"}
            <span className="metric-unit">kg</span>
          </div>
          <div className="metric-foot">
            <MassProvenance weight={weight} />
          </div>
        </div>

        <div className="glass-panel metric">
          <div className="metric-label">Batches recorded</div>
          <div className="metric-value">{stats?.batch_count ?? "--"}</div>
          <div className="metric-foot chips">
            {weight ? `${weight.batches_with_weight} carry a mass reading` : ""}
          </div>
        </div>

        <div className="glass-panel metric">
          <div className="metric-label">Classes detected</div>
          <div className="metric-value">{health?.classes?.length ?? "--"}</div>
          <div className="metric-foot chips">{health?.classes?.join(" · ")}</div>
        </div>
      </div>

      <section className="glass-panel">
        <div className="section-head">
          <h2 className="section-title">Ledger</h2>
          <span className="section-note">
            {batches.length} closed {batches.length === 1 ? "batch" : "batches"} · select a row for
            the full record
          </span>
        </div>
        <div className="table-scroll">
          <table className="ledger-table">
            <thead>
              <tr>
                <th>Batch</th>
                <th>Closed</th>
                <th className="num">Objects</th>
                <th className="num">Mean conf.</th>
                <th className="num">Mass</th>
                <th>Mass source</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((b) => (
                <tr key={b.batch_id} tabIndex={0} onClick={() => setSelected(b)}>
                  <td style={{ color: "var(--gold)" }}>{b.batch_id}</td>
                  <td>{clock(b.timestamp)}</td>
                  <td className="num">{b.total_objects}</td>
                  <td className="num">{pct(b.average_confidence)}</td>
                  <td className="num">{b.weight ? `${kg(b.weight.grams)} kg` : "--"}</td>
                  <td>
                    <WeightBadge weight={b.weight} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {batches.length === 0 && !error && (
            <div className="empty">
              No closed batches yet. Run the demo, or POST frames to /batch/&#123;id&#125;/frame and
              close the batch.
            </div>
          )}
        </div>
      </section>

      {selected && <BatchModal record={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

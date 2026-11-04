import { NetworkBackground } from "./Login.jsx";
import "./portal.css";

// Public product page shown before sign-in. Purely static - it makes no API
// calls, so it works for anonymous visitors.

const FEATURES = [
  {
    icon: "◎",
    title: "L1 → L3 root-cause correlation",
    text:
      "A fiber cut raises one optical alarm and a burst of router-side symptoms. A Neo4j topology graph maps every interface to the fiber span carrying it, so the cut becomes a single root-cause incident and the symptoms are suppressed under it.",
  },
  {
    icon: "⚡",
    title: "Safe, auditable auto-heal",
    text:
      "Rule-based playbooks bounce interfaces, reset BGP sessions or restart processes. The running-config is always backed up first. Every action is logged, and a fix only counts once the device sends its clear trap.",
  },
  {
    icon: "📍",
    title: "Fault location down to the span",
    text:
      "Fiber-cut alarms are narrowed to the span between two repeaters, with km marks and coordinates. Field crews know which repeaters to inspect before they leave the depot.",
  },
  {
    icon: "◉",
    title: "Customer blast radius",
    text:
      "Each service-affecting incident shows which customers are down and which are degraded right now, calculated from live router and BGP state. The queue is prioritised by business impact as well as severity.",
  },
  {
    icon: "▦",
    title: "Human Review board",
    text:
      "Triage, Acknowledge, Dispatch, Resolve. Incidents a person needs to handle are ranked P1–P4 and kept live over WebSocket. Dispatch emails link straight back to the incident.",
  },
  {
    icon: "🗺",
    title: "Live world map of the fleet",
    text:
      "Backbone routers, BGP mesh, customer uplinks and SMF repeaters are drawn on one map. Routers with an unhandled incident blink until someone picks them up.",
  },
];

const AUDIENCES = [
  {
    tag: "Best fit",
    title: "Regional & national telcos and ISPs",
    text:
      "Operators running a few hundred backbone routers and thousands of single-homed CPEs. One fiber cut can trigger dozens of alarms and take a whole region of customers offline. They gain the most from correlation and blast-radius scoring.",
    points: ["Hundreds of PE/P routers", "Thousands of last-mile CPEs", "Lean NOC, 24/7 SLAs"],
    highlight: true,
  },
  {
    tag: "Operations",
    title: "Network Operations Centers",
    text:
      "Shift teams that face alarm storms. Root causes and symptoms are separated, routine faults are fixed automatically, and engineers get a short, prioritised queue instead of a stream of traps.",
    points: ["Less alarm fatigue", "Clear ownership per incident", "Full audit trail"],
  },
  {
    tag: "Services",
    title: "Managed network service providers",
    text:
      "Providers that run WAN and access networks for many customers. Every incident shows which customers it affects, so they can communicate proactively and report against SLAs with evidence.",
    points: ["Per-customer impact", "Proactive notifications", "SLA evidence"],
  },
];

const PIPELINE = [
  { step: "01", title: "Mediation", text: "Receives SNMP traps from the routers and publishes them to Kafka." },
  { step: "02", title: "Normalization", text: "Maps vendor OIDs to a common alarm model with a layer (L1 / L3)." },
  { step: "03", title: "Correlation", text: "Walks the Neo4j topology graph to link symptoms to their root cause." },
  { step: "04", title: "Remediation", text: "Backs up the config, runs or declines a playbook, and confirms the fix." },
  { step: "05", title: "Human review", text: "Sends what needs a person to a live, prioritised board." },
];

function HeroVisual() {
  // A stylised fiber cut: one L1 root cause, two suppressed L3 symptoms.
  return (
    <div className="portal-visual" aria-hidden="true">
      <svg viewBox="0 0 420 300" className="portal-visual-svg">
        <defs>
          <linearGradient id="pv-link" x1="0" x2="1">
            <stop offset="0" stopColor="#3b82f6" stopOpacity="0.9" />
            <stop offset="1" stopColor="#22d3ee" stopOpacity="0.9" />
          </linearGradient>
        </defs>
        {/* healthy mesh */}
        <g stroke="url(#pv-link)" strokeWidth="2" opacity="0.55">
          <line x1="70" y1="70" x2="210" y2="50" />
          <line x1="210" y1="50" x2="350" y2="90" />
          <line x1="70" y1="70" x2="110" y2="210" />
          <line x1="350" y1="90" x2="320" y2="230" />
          <line x1="210" y1="50" x2="320" y2="230" />
          <line x1="110" y1="210" x2="320" y2="230" />
        </g>
        {/* the cut link */}
        <line x1="110" y1="210" x2="210" y2="50" stroke="#f87171" strokeWidth="2.5" strokeDasharray="6 6" className="pv-cut" />
        <circle cx="143" cy="157" r="4" fill="#94a3b8" />
        <circle cx="177" cy="103" r="4" fill="#94a3b8" />
        <g className="pv-fault">
          <circle cx="160" cy="130" r="14" fill="none" stroke="#f87171" strokeWidth="2" />
          <path d="M154 124 L166 136 M166 124 L154 136" stroke="#f87171" strokeWidth="2.5" strokeLinecap="round" />
        </g>
        {/* customer leaves */}
        <g fill="#334155">
          {[[40, 250], [80, 270], [140, 268], [290, 275], [350, 268], [385, 140], [395, 60], [30, 120]].map(([x, y]) => (
            <circle key={`${x}-${y}`} cx={x} cy={y} r="3.5" />
          ))}
        </g>
        <g stroke="#334155" strokeWidth="1">
          <line x1="110" y1="210" x2="40" y2="250" />
          <line x1="110" y1="210" x2="80" y2="270" />
          <line x1="110" y1="210" x2="140" y2="268" />
          <line x1="320" y1="230" x2="290" y2="275" />
          <line x1="320" y1="230" x2="350" y2="268" />
          <line x1="350" y1="90" x2="385" y2="140" />
          <line x1="350" y1="90" x2="395" y2="60" />
          <line x1="70" y1="70" x2="30" y2="120" />
        </g>
        {/* primaries */}
        {[[70, 70, "ok"], [210, 50, "warn"], [350, 90, "ok"], [110, 210, "warn"], [320, 230, "ok"]].map(([x, y, s]) => (
          <g key={`${x}-${y}`}>
            {s === "warn" && <circle cx={x} cy={y} r="13" className="pv-pulse" />}
            <circle cx={x} cy={y} r="8" fill={s === "warn" ? "#f59e0b" : "#22c55e"} stroke="#0f1420" strokeWidth="2" />
          </g>
        ))}
      </svg>

      <div className="pv-card pv-card-root">
        <span className="pv-badge pv-badge-l1">L1 · ROOT CAUSE</span>
        <strong>Fiber cut · span R3 → R4</strong>
        <small>km 41.2 – 58.7 · 38 customers degraded</small>
      </div>
      <div className="pv-card pv-card-sym">
        <span className="pv-badge pv-badge-l3">L3 · SUPPRESSED</span>
        <strong>2 × IS-IS adjacency down</strong>
        <small>symptoms linked · no auto-heal</small>
      </div>
    </div>
  );
}

export default function Portal({ onLoginClick }) {
  const goLogin = (e) => {
    e.preventDefault();
    onLoginClick();
  };

  return (
    <div className="portal">
      <nav className="portal-nav">
        <div className="portal-nav-inner">
          <a href="#top" className="portal-brand">
            <span className="portal-logo" aria-hidden="true">
              <span />
            </span>
            Network Control Room
          </a>
          <div className="portal-nav-links">
            <a href="#features">Platform</a>
            <a href="#how">How it works</a>
            <a href="#customers">Who it's for</a>
          </div>
          <a href="/login" className="portal-btn portal-btn-primary portal-nav-login" onClick={goLogin}>
            Log in
          </a>
        </div>
      </nav>

      <header className="portal-hero" id="top">
        <NetworkBackground />
        <div className="portal-hero-glow" aria-hidden="true" />
        <div className="portal-hero-inner">
          <div className="portal-hero-copy">
            <span className="portal-eyebrow">Network assurance for telecom operators</span>
            <h1>
              From alarm storm to <span className="portal-grad">one root cause</span>, automatically.
            </h1>
            <p className="portal-lead">
              Network Control Room watches your router fleet in real time. It links physical-layer faults to the
              control-plane symptoms they cause, fixes routine problems safely, and shows your engineers which
              customers are affected and where to send the crew.
            </p>
            <div className="portal-cta-row">
              <a href="/login" className="portal-btn portal-btn-primary portal-btn-lg" onClick={goLogin}>
                Log in to the console →
              </a>
              <a href="#features" className="portal-btn portal-btn-ghost portal-btn-lg">
                Explore the platform
              </a>
            </div>
          </div>
          <HeroVisual />
        </div>
      </header>

      <section className="portal-strip">
        <div className="portal-strip-inner">
          <div><strong>L1 + L3</strong><span>layered alarm model</span></div>
          <div><strong>Real time</strong><span>Kafka event pipeline</span></div>
          <div><strong>Backup-first</strong><span>every auto-heal action</span></div>
          <div><strong>P1–P4</strong><span>priority from customer impact</span></div>
        </div>
      </section>

      <section className="portal-section portal-problem">
        <div className="portal-container portal-split">
          <div>
            <span className="portal-eyebrow">The problem</span>
            <h2>One cut fiber. Forty alarms. Six engineers chasing the wrong router.</h2>
          </div>
          <div className="portal-problem-text">
            <p>
              When a physical span fails, every router that uses it reports its own symptoms: interfaces flapping,
              adjacencies dropping, BGP sessions resetting. Traditional monitoring shows each one as a separate
              problem. Automation then makes things worse by bouncing interfaces that were never broken.
            </p>
            <p>
              Network Control Room knows which fiber carries which interface. It raises <em>one</em> incident for
              the cut, suppresses the symptoms beneath it, and keeps automation away from anything a config push
              can't fix.
            </p>
          </div>
        </div>
      </section>

      <section className="portal-section" id="features">
        <div className="portal-container">
          <div className="portal-section-head">
            <span className="portal-eyebrow">Platform</span>
            <h2>Everything a NOC needs, from the first trap to the closed incident</h2>
          </div>
          <div className="portal-grid">
            {FEATURES.map((f) => (
              <article key={f.title} className="portal-card">
                <div className="portal-card-icon" aria-hidden="true">{f.icon}</div>
                <h3>{f.title}</h3>
                <p>{f.text}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="portal-section portal-alt" id="how">
        <div className="portal-container">
          <div className="portal-section-head">
            <span className="portal-eyebrow">How it works</span>
            <h2>A deterministic pipeline you can audit</h2>
            <p className="portal-section-sub">
              Every decision comes from explicit rules and your real topology. There are no black-box models.
              Each step runs as its own service, so the pipeline scales with your fleet.
            </p>
          </div>
          <ol className="portal-pipeline">
            {PIPELINE.map((p) => (
              <li key={p.step}>
                <span className="portal-step">{p.step}</span>
                <h3>{p.title}</h3>
                <p>{p.text}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="portal-section" id="customers">
        <div className="portal-container">
          <div className="portal-section-head">
            <span className="portal-eyebrow">Who it's for</span>
            <h2>Built for operators whose customers notice every minute of downtime</h2>
          </div>
          <div className="portal-audience">
            {AUDIENCES.map((a) => (
              <article key={a.title} className={`portal-aud-card${a.highlight ? " is-highlight" : ""}`}>
                <span className="portal-aud-tag">{a.tag}</span>
                <h3>{a.title}</h3>
                <p>{a.text}</p>
                <ul>
                  {a.points.map((pt) => (
                    <li key={pt}>{pt}</li>
                  ))}
                </ul>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="portal-section portal-alt">
        <div className="portal-container portal-benefits">
          <div>
            <span className="portal-eyebrow">Why teams choose it</span>
            <h2>Less noise. Faster fixes. Clearer accountability.</h2>
          </div>
          <ul className="portal-benefit-list">
            <li><strong>Shorter time to resolve:</strong> engineers start from the root cause, not from the loudest symptom.</li>
            <li><strong>Fewer truck rolls:</strong> crews are sent to the exact span between two repeaters.</li>
            <li><strong>Automation you can trust:</strong> config is backed up before every change, and hardware, security and fiber faults are never touched.</li>
            <li><strong>Customer-first priorities:</strong> outages that take customers offline go to the top of the queue.</li>
            <li><strong>Secure by default:</strong> role-based access (viewer, operator, admin) and two-factor sign-in.</li>
          </ul>
        </div>
      </section>

      <section className="portal-final">
        <div className="portal-container portal-final-inner">
          <h2>Your fleet is already talking. Start listening to the root cause.</h2>
          <a href="/login" className="portal-btn portal-btn-primary portal-btn-lg" onClick={goLogin}>
            Log in to Network Control Room →
          </a>
        </div>
      </section>

      <footer className="portal-footer">
        <div className="portal-container portal-footer-inner">
          <span>© {new Date().getFullYear()} Network Control Room</span>
          <span>Network assurance · Root-cause correlation · Auto-heal</span>
        </div>
      </footer>
    </div>
  );
}

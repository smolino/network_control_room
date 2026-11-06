import { useState } from "react";
import { NetworkBackground } from "./Login.jsx";
import "./portal.css";

const CONTACT_EMAIL = "controlroom@point2point.org.uk";
const CONTACT_HREF = `mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent("Network Control Room - product information")}`;

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
    icon: "〰",
    title: "Dark-fibre DWDM corridors",
    text:
      "Model the real optical line: DWDM terminals, in-line amplifier huts, transponders, wavelengths and every rented fibre span, with both directions and the supervisory channel. Build a corridor in a guided form, check it before saving, and export it as JSON for backup.",
  },
  {
    icon: "⚡",
    title: "Optical root-cause engine",
    text:
      "Walks the line in each direction, element by element. It tells a cable cut (both directions) from a single-fibre cut, an amplifier failure, a lost wavelength, a bad patch cord or a supervisory-channel fault, and links every other alarm to that one root.",
  },
  {
    icon: "⛭",
    title: "Alarm classes and filtering",
    text:
      "Every alarm is classed as transport, logical, equipment or management. Hide the hardware and environment noise - fans, power, temperature - so the queue shows the faults on the fibre network.",
  },
  {
    icon: "🔒",
    title: "Secure, role-based access",
    text:
      "Viewer, operator and admin roles are enforced on the server, with two-factor sign-in. Every acknowledgement, dispatch and resolution is recorded with who did it, the reason and a note.",
  },
  {
    icon: "📍",
    title: "Fault location down to the span",
    text:
      "Faults are narrowed to the span between two amplifier sites, with the fibre provider, its circuit ID, km marks and coordinates. The guidance says whether to open a provider ticket and run an OTDR test, or send a crew to a named site.",
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
      "Backbone routers, BGP mesh, customer uplinks and optical corridors are drawn on one map, with L1 and L3 layers. A cut span turns red with both ends ringed, and the router links it took down are shown dashed.",
  },
];

const AUDIENCES = [
  {
    tag: "Best fit",
    title: "Regional & national telcos and ISPs",
    text:
      "Operators running a few hundred backbone routers and thousands of single-homed CPEs. One fiber cut can trigger dozens of alarms and take a whole region of customers offline. They gain the most from correlation and blast-radius scoring.",
    points: ["Hundreds of PE/P routers", "DWDM over rented dark fibre", "Thousands of last-mile CPEs", "Lean NOC, 24/7 SLAs"],
    highlight: true,
  },
  {
    tag: "Operations",
    title: "Network Operations Centers",
    text:
      "Shift teams that face alarm storms. Root causes and symptoms are separated, and engineers get a short, prioritised queue instead of a stream of traps.",
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
  { step: "02", title: "Normalization", text: "Maps vendor OIDs to a common alarm model with a layer (L1 / L3) and an alarm class, and filters out the classes you hide." },
  { step: "03", title: "Correlation", text: "Walks the optical line in each direction and the Neo4j topology graph to link symptoms to their root cause." },
  { step: "04", title: "Human review", text: "Sends what needs a person to a live, prioritised board." },
];

const VIDEOS = [
  {
    id: "wLHMmDIcE94",
    tag: "Product demo",
    title: "AI-free Telco NOC demo: L1/L3 root-cause correlation",
    text: "Walks through the console: a fiber cut is correlated to its symptoms, the crew is pointed to the faulty span, and incidents move through the Human Review board.",
  },
  {
    id: "ykB87Gxih6w",
    tag: "Architecture",
    title: "Telco NOC architecture explained (Kafka + Neo4j)",
    text: "How mediation, normalization and correlation fit together, and how the topology graph finds the root cause.",
  },
  {
    id: "D1Bx9AOBLm8",
    tag: "MIBs & Traps",
    title: "Import Juniper MIBs & create custom SNMP traps",
    text: "Teach the control room a new vendor: import MIBs, turn a trap into a custom rule, and watch a live trap open a prioritised incident and clear it.",
  },
  {
    id: "shKbDUYNYKA",
    tag: "Optical corridors",
    title: "How to create an optical corridor (DWDM)",
    text: "Build a Madrid to Valencia DWDM corridor from the UI - sites, fibre spans, amplifiers, transponders and wavelengths - then cut a span and watch the root-cause engine raise one cable-cut incident.",
  },
];

// Plays inside the page: shows the thumbnail until clicked, then loads the
// privacy-enhanced (youtube-nocookie) player in place - no redirect to YouTube.
function VideoPlayer({ id, title }) {
  const [playing, setPlaying] = useState(false);
  return (
    <div className="portal-video-frame">
      {playing ? (
        <iframe
          src={`https://www.youtube-nocookie.com/embed/${id}?autoplay=1&rel=0&modestbranding=1&playsinline=1`}
          title={title}
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
          referrerPolicy="strict-origin-when-cross-origin"
          allowFullScreen
        />
      ) : (
        <button type="button" className="portal-video-poster" onClick={() => setPlaying(true)} aria-label={`Play: ${title}`}>
          <img src={`https://i.ytimg.com/vi/${id}/hqdefault.jpg`} alt="" loading="lazy" />
          <span className="portal-video-play" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="28" height="28"><path d="M8 5v14l11-7z" fill="currentColor" /></svg>
          </span>
        </button>
      )}
    </div>
  );
}

function HeroVisual() {
  // A stylised cable cut: one L1 root cause, its symptoms linked under it.
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
        <strong>Cable cut · span 6, amp → amp</strong>
        <small>both directions dark · km 41.2 – 58.7</small>
      </div>
      <div className="pv-card pv-card-sym">
        <span className="pv-badge pv-badge-l3">SYMPTOMS · LINKED</span>
        <strong>66 alarms · LOS, IS-IS, BGP</strong>
        <small>linked to the cable cut</small>
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
            <a href="#videos">Videos</a>
            <a href="#features">Platform</a>
            <a href="#how">How it works</a>
            <a href="#customers">Who it's for</a>
            <a href="#contact">Contact</a>
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
              Network Control Room watches your routers and your DWDM optical line in real time. It traces a fault
              along the fibre to the span or amplifier that caused it, links the control-plane symptoms to it, and
              shows your engineers which customers are affected and where to send the crew.
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
          <div><strong>DWDM-aware</strong><span>spans, amplifiers, wavelengths</span></div>
          <div><strong>1 incident</strong><span>per cut, every alarm linked</span></div>
          <div><strong>Span-level</strong><span>fiber fault location</span></div>
          <div><strong>P1–P4</strong><span>priority from customer impact</span></div>
        </div>
      </section>

      <section className="portal-section portal-videos" id="videos">
        <div className="portal-container">
          <div className="portal-section-head">
            <span className="portal-eyebrow">See it in action</span>
            <h2>Watch Network Control Room at work</h2>
            <p className="portal-section-sub">
              A guided demo of the console, a walkthrough of the architecture behind it, how to onboard any vendor's SNMP traps, and how to model an optical corridor.
            </p>
          </div>
          <div className="portal-video-grid">
            {VIDEOS.map((v) => (
              <article key={v.id} className="portal-video-card">
                <VideoPlayer id={v.id} title={v.title} />
                <div className="portal-video-body">
                  <span className="portal-aud-tag">{v.tag}</span>
                  <h3>{v.title}</h3>
                  <p>{v.text}</p>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="portal-section portal-alt portal-problem">
        <div className="portal-container portal-split">
          <div>
            <span className="portal-eyebrow">The problem</span>
            <h2>One cut fiber. Forty alarms. Six engineers chasing the wrong router.</h2>
          </div>
          <div className="portal-problem-text">
            <p>
              When a fibre span fails, every amplifier and transponder downstream loses light and every router
              that uses it reports its own symptoms: interfaces flapping, adjacencies dropping, BGP sessions
              resetting. Traditional monitoring shows each one as a separate
              problem, and engineers lose time investigating routers that were never broken.
            </p>
            <p>
              Network Control Room knows the optical path of every wavelength and which fibre carries which
              interface. It raises <em>one</em> incident for the cut, links the symptoms beneath it, and tells the
              field crew exactly which span to repair - and which provider to call.
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
            <li><strong>Fewer truck rolls:</strong> crews are sent to the exact span between two amplifier sites.</li>
            <li><strong>Faster provider tickets:</strong> a cut on rented fibre comes with the provider, circuit ID and km marks ready to send.</li>
            <li><strong>Less noise:</strong> hardware and environment alarms can be filtered out, so the queue is about the network.</li>
            <li><strong>Clear accountability:</strong> every incident shows who owns it, which team it was dispatched to, and how it was resolved.</li>
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

      <section className="portal-section portal-alt portal-contact" id="contact">
        <div className="portal-container portal-contact-inner">
          <div>
            <span className="portal-eyebrow">Contact</span>
            <h2>Want to know more about the product?</h2>
            <p className="portal-section-sub">
              Ask for a demo, pricing, or how Network Control Room fits your network. We'll get back to you.
            </p>
          </div>
          <a href={CONTACT_HREF} className="portal-contact-card">
            <span className="portal-contact-icon" aria-hidden="true">✉</span>
            <span>
              <small>Email us</small>
              <strong>{CONTACT_EMAIL}</strong>
            </span>
          </a>
        </div>
      </section>

      <footer className="portal-footer">
        <div className="portal-container portal-footer-inner">
          <span>© {new Date().getFullYear()} Network Control Room</span>
          <span>Network assurance · Optical root cause · Customer impact</span>
          <a href={CONTACT_HREF} className="portal-footer-mail">{CONTACT_EMAIL}</a>
        </div>
      </footer>
    </div>
  );
}

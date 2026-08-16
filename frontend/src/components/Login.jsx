import { useEffect, useRef, useState } from "react";
import { verifyTOTP } from "../totp.js";
import { verifyCredentials } from "../users.js";

const NODE_COUNT = 70;
const LINK_DISTANCE = 140;
const NODE_COLOR = "96, 165, 250";
const LINK_COLOR = "96, 165, 250";

function NetworkBackground() {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    let width, height, nodes, frameId;

    const resize = () => {
      width = canvas.width = canvas.offsetWidth * window.devicePixelRatio;
      height = canvas.height = canvas.offsetHeight * window.devicePixelRatio;
    };

    const makeNodes = () =>
      Array.from({ length: NODE_COUNT }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.35 * window.devicePixelRatio,
        vy: (Math.random() - 0.5) * 0.35 * window.devicePixelRatio,
      }));

    resize();
    nodes = makeNodes();

    const onResize = () => {
      resize();
      nodes = makeNodes();
    };
    window.addEventListener("resize", onResize);

    const step = () => {
      ctx.clearRect(0, 0, width, height);

      for (const n of nodes) {
        n.x += n.vx;
        n.y += n.vy;
        if (n.x <= 0 || n.x >= width) n.vx *= -1;
        if (n.y <= 0 || n.y >= height) n.vy *= -1;
      }

      const linkDist = LINK_DISTANCE * window.devicePixelRatio;
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const dx = nodes[i].x - nodes[j].x;
          const dy = nodes[i].y - nodes[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < linkDist) {
            const opacity = (1 - dist / linkDist) * 0.5;
            ctx.strokeStyle = `rgba(${LINK_COLOR}, ${opacity})`;
            ctx.lineWidth = window.devicePixelRatio;
            ctx.beginPath();
            ctx.moveTo(nodes[i].x, nodes[i].y);
            ctx.lineTo(nodes[j].x, nodes[j].y);
            ctx.stroke();
          }
        }
      }

      for (const n of nodes) {
        ctx.fillStyle = `rgba(${NODE_COLOR}, 0.85)`;
        ctx.beginPath();
        ctx.arc(n.x, n.y, 2 * window.devicePixelRatio, 0, Math.PI * 2);
        ctx.fill();
      }

      frameId = requestAnimationFrame(step);
    };
    frameId = requestAnimationFrame(step);

    return () => {
      cancelAnimationFrame(frameId);
      window.removeEventListener("resize", onResize);
    };
  }, []);

  return <canvas ref={canvasRef} className="login-canvas" />;
}

export default function Login({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [otpCode, setOtpCode] = useState("");
  const [pendingUser, setPendingUser] = useState(null);
  const [error, setError] = useState("");

  const handleCredentialsSubmit = (e) => {
    e.preventDefault();
    const user = verifyCredentials(username, password);
    if (!user) {
      setError("Invalid username or password");
      return;
    }
    setError("");
    if (user.otpSecret) {
      setPendingUser(user);
    } else {
      onLogin(user.username);
    }
  };

  const handleOtpSubmit = async (e) => {
    e.preventDefault();
    try {
      const ok = await verifyTOTP(pendingUser.otpSecret, otpCode);
      if (ok) {
        setError("");
        onLogin(pendingUser.username);
      } else {
        setError("Invalid authentication code");
      }
    } catch (err) {
      setError(`Couldn't verify that code: ${err.message}`);
    }
  };

  return (
    <div className="login-page">
      <NetworkBackground />
      {!pendingUser ? (
        <form className="login-card" onSubmit={handleCredentialsSubmit}>
          <h1>Network Control Room</h1>
          <p className="login-subtitle">Sign in to access the fleet</p>

          <label>
            Username
            <input
              type="text"
              value={username}
              autoFocus
              autoComplete="username"
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>

          <label>
            Password
            <input
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>

          {error && <div className="login-error">{error}</div>}

          <button type="submit" className="login-submit">
            Sign in
          </button>
        </form>
      ) : (
        <form className="login-card" onSubmit={handleOtpSubmit}>
          <h1>Network Control Room</h1>
          <p className="login-subtitle">Enter your 6-digit authentication code</p>

          <label>
            Authentication code
            <input
              type="text"
              inputMode="numeric"
              maxLength={6}
              autoFocus
              value={otpCode}
              onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, ""))}
            />
          </label>

          {error && <div className="login-error">{error}</div>}

          <button type="submit" className="login-submit">
            Verify
          </button>
        </form>
      )}
    </div>
  );
}

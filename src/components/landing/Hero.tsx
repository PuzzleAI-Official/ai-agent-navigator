import { motion, useMotionValue, useTransform, useSpring, useAnimationFrame } from "framer-motion";
import { useEffect, useRef, useState, useCallback } from "react";

/* ─── Network Constellation — cursor-interactive ─── */
const NetworkConstellation = ({ mouseX, mouseY }: { mouseX: number; mouseY: number }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frame = useRef(0);
  const nodesRef = useRef<Array<{ x: number; y: number; vx: number; vy: number; r: number; baseX: number; baseY: number }>>([]);
  const initialized = useRef(false);

  useAnimationFrame(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    frame.current += 0.008;
    const t = frame.current;

    // Initialize nodes once
    if (!initialized.current || nodesRef.current.length === 0) {
      nodesRef.current = Array.from({ length: 60 }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.3,
        vy: (Math.random() - 0.5) * 0.3,
        r: 1 + Math.random() * 2.5,
        baseX: Math.random() * w,
        baseY: Math.random() * h,
      }));
      initialized.current = true;
    }

    const nodes = nodesRef.current;
    const mx = mouseX * w;
    const my = mouseY * h;

    // Update & draw nodes
    nodes.forEach((node, i) => {
      // Gentle drift
      node.x = node.baseX + Math.sin(t + i * 0.5) * 30 + Math.cos(t * 0.7 + i * 0.3) * 20;
      node.y = node.baseY + Math.cos(t + i * 0.4) * 25 + Math.sin(t * 0.6 + i * 0.5) * 15;

      // Mouse repulsion
      const dx = node.x - mx;
      const dy = node.y - my;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 150 && dist > 0) {
        const force = (150 - dist) / 150;
        node.x += (dx / dist) * force * 20;
        node.y += (dy / dist) * force * 20;
      }

      // Keep in bounds
      node.x = Math.max(0, Math.min(w, node.x));
      node.y = Math.max(0, Math.min(h, node.y));
    });

    // Draw connections
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[i].x - nodes[j].x;
        const dy = nodes[i].y - nodes[j].y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 120) {
          const alpha = (1 - dist / 120) * 0.08;
          ctx.beginPath();
          ctx.moveTo(nodes[i].x, nodes[i].y);
          ctx.lineTo(nodes[j].x, nodes[j].y);
          ctx.strokeStyle = `rgba(30, 30, 30, ${alpha})`;
          ctx.lineWidth = 0.5;
          ctx.stroke();
        }
      }
    }

    // Draw nodes
    nodes.forEach((node, i) => {
      // Outer glow for larger nodes
      if (node.r > 2) {
        const gradient = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, node.r * 6);
        gradient.addColorStop(0, `rgba(210, 100, 60, 0.06)`);
        gradient.addColorStop(1, `rgba(210, 100, 60, 0)`);
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.r * 6, 0, Math.PI * 2);
        ctx.fillStyle = gradient;
        ctx.fill();
      }

      // Core
      ctx.beginPath();
      ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
      const isAccent = i % 7 === 0;
      ctx.fillStyle = isAccent
        ? `rgba(210, 100, 60, ${0.4 + Math.sin(t + i) * 0.15})`
        : `rgba(30, 30, 30, ${0.08 + node.r * 0.03})`;
      ctx.fill();
    });

    // Mouse cursor glow
    if (mx > 0 && my > 0) {
      const gradient = ctx.createRadialGradient(mx, my, 0, mx, my, 80);
      gradient.addColorStop(0, "rgba(210, 100, 60, 0.04)");
      gradient.addColorStop(1, "rgba(210, 100, 60, 0)");
      ctx.beginPath();
      ctx.arc(mx, my, 80, 0, Math.PI * 2);
      ctx.fillStyle = gradient;
      ctx.fill();
    }

    // Decorative arcs
    ctx.beginPath();
    ctx.ellipse(w * 0.5, h * 0.5, w * 0.35, h * 0.25, -0.1, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(30, 30, 30, 0.02)";
    ctx.lineWidth = 0.5;
    ctx.stroke();

    ctx.beginPath();
    ctx.ellipse(w * 0.5, h * 0.5, w * 0.22, h * 0.15, 0.2, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(30, 30, 30, 0.025)";
    ctx.stroke();
  });

  return (
    <motion.canvas
      ref={canvasRef}
      className="absolute inset-0 w-full h-full"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 2, delay: 0.3 }}
    />
  );
};

/* ─── Typing effect ─── */
const TypingText = ({ text, delay = 0 }: { text: string; delay?: number }) => {
  const [displayed, setDisplayed] = useState("");
  const [started, setStarted] = useState(false);

  useEffect(() => {
    const timeout = setTimeout(() => setStarted(true), delay * 1000);
    return () => clearTimeout(timeout);
  }, [delay]);

  useEffect(() => {
    if (!started) return;
    let i = 0;
    const interval = setInterval(() => {
      setDisplayed(text.slice(0, i + 1));
      i++;
      if (i >= text.length) clearInterval(interval);
    }, 22);
    return () => clearInterval(interval);
  }, [started, text]);

  return (
    <span>
      {displayed}
      {started && displayed.length < text.length && (
        <span className="animate-pulse text-accent">|</span>
      )}
    </span>
  );
};

const Hero = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [mousePos, setMousePos] = useState({ x: 0.5, y: 0.5 });

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    setMousePos({
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    });
  }, []);

  return (
    <section
      ref={containerRef}
      onMouseMove={handleMouseMove}
      className="relative min-h-screen overflow-hidden cursor-crosshair"
    >
      {/* Full-bleed interactive constellation */}
      <NetworkConstellation mouseX={mousePos.x} mouseY={mousePos.y} />

      {/* Gradient washes */}
      <div className="absolute top-0 left-0 w-[50%] h-[60%] bg-[radial-gradient(ellipse_at_20%_30%,hsl(15_80%_55%/0.04),transparent_60%)]" />
      <div className="absolute bottom-0 right-0 w-[40%] h-[40%] bg-[radial-gradient(ellipse_at_80%_80%,hsl(36_40%_80%/0.08),transparent_60%)]" />

      {/* Content overlay */}
      <div className="relative z-10 min-h-screen flex flex-col justify-end pb-16 md:pb-24">
        <div className="max-w-[1400px] mx-auto w-full px-8">
          {/* Large type block — positioned over the constellation */}
          <div className="mb-auto pt-40 md:pt-48">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.3, duration: 0.8 }}
              className="flex items-center gap-4 mb-8"
            >
              <motion.div
                className="w-12 h-px bg-accent"
                initial={{ scaleX: 0 }}
                animate={{ scaleX: 1 }}
                transition={{ delay: 0.5, duration: 0.8 }}
                style={{ transformOrigin: "left" }}
              />
              <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
                The AI hiring platform
              </span>
            </motion.div>

            <motion.h1
              initial={{ opacity: 0, y: 60 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 1.2, delay: 0.3, ease: [0.16, 1, 0.3, 1] }}
              className="font-display text-[clamp(3.5rem,10vw,9rem)] leading-[0.85] tracking-[-0.04em] text-foreground max-w-5xl"
            >
              Find the
              <br />
              <span className="italic text-gradient">right</span> AI.
            </motion.h1>

            <motion.p
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, delay: 0.9 }}
              className="mt-8 text-base md:text-lg text-muted-foreground max-w-md leading-relaxed"
            >
              <TypingText
                text="Describe your workflow. We test 200+ AI solutions against your real use cases — and deliver three verdicts."
                delay={1.5}
              />
            </motion.p>

            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 1.2 }}
              className="mt-10 flex flex-col sm:flex-row items-start sm:items-center gap-5"
            >
              <a
                href="#start"
                className="group relative inline-flex items-center gap-3 bg-foreground text-background px-7 py-3.5 font-mono text-[12px] uppercase tracking-[0.12em] overflow-hidden transition-all duration-300 hover:shadow-[0_8px_30px_-8px_hsl(var(--foreground)/0.3)]"
              >
                <span className="relative z-10">Try PuzzleAI</span>
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="relative z-10 transition-transform duration-300 group-hover:translate-x-1">
                  <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
                <div className="absolute inset-0 bg-gradient-to-r from-transparent via-background/10 to-transparent -translate-x-full group-hover:translate-x-full transition-transform duration-700" />
              </a>
              <a
                href="#how-it-works"
                className="group font-mono text-[12px] uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors flex items-center gap-2"
              >
                <span className="border-b border-muted-foreground/30 pb-0.5 group-hover:border-foreground/50 transition-colors">See how it works</span>
                <motion.span
                  animate={{ y: [0, 3, 0] }}
                  transition={{ repeat: Infinity, duration: 1.5 }}
                  className="text-accent"
                >↓</motion.span>
              </a>
            </motion.div>
          </div>

          {/* Bottom strip — three metrics with animated counters */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 2, duration: 1 }}
            className="mt-16 pt-6 border-t border-border/60 flex flex-wrap items-end justify-between"
          >
            <div className="flex gap-12 md:gap-16">
              {[
                { num: "01", label: "Performance", desc: "use case success rate" },
                { num: "02", label: "Speed", desc: "real-world latency" },
                { num: "03", label: "Cost", desc: "actual token pricing" },
              ].map((m) => (
                <div key={m.label} className="group cursor-default">
                  <span className="font-mono text-[8px] text-accent/50 block mb-0.5">{m.num}</span>
                  <span className="font-grotesk font-semibold text-xs tracking-tight group-hover:text-accent transition-colors duration-300">{m.label}</span>
                  <span className="hidden md:block font-mono text-[9px] text-muted-foreground/40 mt-0.5">{m.desc}</span>
                </div>
              ))}
            </div>

            {/* Floating eval badge */}
            <motion.div
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: 2.2, duration: 0.6 }}
              className="hidden md:flex items-center gap-3 border border-border/60 bg-background/60 backdrop-blur-sm px-4 py-2"
            >
              <div className="w-1.5 h-1.5 bg-accent animate-pulse" />
              <span className="font-mono text-[9px] text-muted-foreground tracking-wider">4,291 evaluations completed</span>
            </motion.div>
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default Hero;

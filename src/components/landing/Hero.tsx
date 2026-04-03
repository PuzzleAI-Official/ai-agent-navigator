import { motion, useAnimationFrame } from "framer-motion";
import { useEffect, useRef, useState, useCallback } from "react";

/* ─── Flowing Field Visualization — generative topographic art ─── */
const FlowField = ({ mouseX, mouseY }: { mouseX: number; mouseY: number }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frame = useRef(0);
  const particles = useRef<Array<{ x: number; y: number; life: number; maxLife: number; speed: number; hue: number }>>([]);

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

    frame.current += 0.003;
    const t = frame.current;

    // Fade previous frame — creates trailing effect
    ctx.fillStyle = "hsl(40, 33%, 97%)";
    ctx.globalAlpha = 0.04;
    ctx.fillRect(0, 0, w, h);
    ctx.globalAlpha = 1;

    // Spawn particles
    while (particles.current.length < 300) {
      particles.current.push({
        x: Math.random() * w,
        y: Math.random() * h,
        life: 0,
        maxLife: 200 + Math.random() * 400,
        speed: 0.3 + Math.random() * 0.8,
        hue: Math.random() > 0.85 ? 225 : 230, // mostly ink, some accent
      });
    }

    const mx = mouseX * w;
    const my = mouseY * h;

    // Flow field function — creates organic topographic patterns
    const getAngle = (x: number, y: number) => {
      const scale = 0.003;
      const nx = x * scale;
      const ny = y * scale;
      // Layered sine waves create organic flow
      return (
        Math.sin(nx * 1.2 + t * 2) * Math.cos(ny * 0.8 + t * 1.5) +
        Math.sin(nx * 0.5 + ny * 0.7 + t) * 0.8 +
        Math.cos(nx * 2.1 - ny * 0.9 + t * 0.7) * 0.4
      ) * Math.PI;
    };

    // Update and draw particles
    particles.current.forEach((p, i) => {
      const angle = getAngle(p.x, p.y);

      // Mouse influence — gentle attraction creating swirls
      const dx = mx - p.x;
      const dy = my - p.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      let mouseInfluence = 0;
      if (dist < 200 && dist > 0) {
        mouseInfluence = (200 - dist) / 200;
      }

      const finalAngle = angle + (mouseInfluence * Math.atan2(dy, dx) * 0.3);

      p.x += Math.cos(finalAngle) * p.speed;
      p.y += Math.sin(finalAngle) * p.speed;
      p.life++;

      // Lifecycle alpha — fade in and out
      const lifeRatio = p.life / p.maxLife;
      const alpha = lifeRatio < 0.1
        ? lifeRatio / 0.1
        : lifeRatio > 0.9
          ? (1 - lifeRatio) / 0.1
          : 1;

      const finalAlpha = alpha * (p.hue === 225 ? 0.12 : 0.04);

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.hue === 225 ? 1.5 : 0.8, 0, Math.PI * 2);
      ctx.fillStyle = `hsla(${p.hue}, ${p.hue === 225 ? '45%' : '20%'}, ${p.hue === 225 ? '42%' : '30%'}, ${finalAlpha})`;
      ctx.fill();

      // Reset dead or out-of-bounds particles
      if (p.life >= p.maxLife || p.x < -10 || p.x > w + 10 || p.y < -10 || p.y > h + 10) {
        particles.current[i] = {
          x: Math.random() * w,
          y: Math.random() * h,
          life: 0,
          maxLife: 200 + Math.random() * 400,
          speed: 0.3 + Math.random() * 0.8,
          hue: Math.random() > 0.85 ? 225 : 230,
        };
      }
    });

    // Draw topographic contour rings
    ctx.globalAlpha = 0.025;
    for (let ring = 0; ring < 5; ring++) {
      const cx = w * (0.3 + ring * 0.12);
      const cy = h * (0.35 + Math.sin(t + ring) * 0.08);
      const r = 80 + ring * 60 + Math.sin(t * 0.5 + ring) * 20;

      ctx.beginPath();
      ctx.ellipse(cx, cy, r, r * 0.6, ring * 0.3 + t * 0.1, 0, Math.PI * 2);
      ctx.strokeStyle = `hsl(225, 45%, 42%)`;
      ctx.lineWidth = 0.5;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // Cursor glow halo
    if (mx > 0 && my > 0) {
      const gradient = ctx.createRadialGradient(mx, my, 0, mx, my, 120);
      gradient.addColorStop(0, "hsla(225, 45%, 42%, 0.04)");
      gradient.addColorStop(0.5, "hsla(225, 45%, 42%, 0.015)");
      gradient.addColorStop(1, "transparent");
      ctx.beginPath();
      ctx.arc(mx, my, 120, 0, Math.PI * 2);
      ctx.fillStyle = gradient;
      ctx.fill();
    }
  });

  return (
    <motion.canvas
      ref={canvasRef}
      className="absolute inset-0 w-full h-full"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 2.5, delay: 0.3 }}
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
      className="relative min-h-screen overflow-hidden cursor-crosshair noise-overlay"
    >
      {/* Flow field visualization */}
      <FlowField mouseX={mousePos.x} mouseY={mousePos.y} />

      {/* Depth washes */}
      <div className="absolute top-0 left-0 w-[60%] h-[70%] bg-[radial-gradient(ellipse_at_20%_30%,hsl(225_45%_42%/0.04),transparent_60%)] pointer-events-none" />
      <div className="absolute bottom-0 right-0 w-[50%] h-[50%] bg-[radial-gradient(ellipse_at_80%_80%,hsl(30_30%_55%/0.04),transparent_60%)] pointer-events-none" />
      <div className="absolute top-[20%] right-[10%] w-[30%] h-[30%] bg-[radial-gradient(ellipse_at_center,hsl(225_45%_42%/0.02),transparent_50%)] pointer-events-none" />

      {/* Decorative measurement marks — editorial detail */}
      <div className="absolute top-8 left-8 flex flex-col gap-1 opacity-[0.12]">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="flex items-center gap-2">
            <div className={`h-px ${i === 0 || i === 4 ? 'w-6' : 'w-3'} bg-foreground`} />
            {(i === 0 || i === 4) && (
              <span className="font-mono text-[7px] text-foreground">{i === 0 ? '00' : '04'}</span>
            )}
          </div>
        ))}
      </div>

      {/* Right vertical accent line */}
      <motion.div
        className="absolute top-0 right-[12%] w-px h-full bg-gradient-to-b from-transparent via-accent/[0.06] to-transparent pointer-events-none"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 1.5, duration: 2 }}
      />

      {/* Content overlay */}
      <div className="relative z-10 min-h-screen flex flex-col justify-end pb-16 md:pb-24">
        <div className="max-w-[1400px] mx-auto w-full px-8">
          <div className="mb-auto pt-40 md:pt-48">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.3, duration: 0.8 }}
              className="flex items-center gap-4 mb-8"
            >
              <motion.div
                className="w-16 h-px bg-gradient-to-r from-accent to-accent/20"
                initial={{ scaleX: 0 }}
                animate={{ scaleX: 1 }}
                transition={{ delay: 0.5, duration: 1 }}
                style={{ transformOrigin: "left" }}
              />
              <span className="font-mono text-[11px] uppercase tracking-[0.25em] text-muted-foreground">
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
                className="group relative inline-flex items-center gap-3 bg-foreground text-background px-7 py-3.5 font-mono text-[12px] uppercase tracking-[0.12em] overflow-hidden transition-all duration-300 hover:shadow-[0_12px_40px_-12px_hsl(225_45%_42%/0.4)] shimmer-hover"
              >
                <span className="relative z-10">Try PuzzleAI</span>
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="relative z-10 transition-transform duration-300 group-hover:translate-x-1">
                  <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </a>
              <a
                href="#how-it-works"
                className="group font-mono text-[12px] uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors flex items-center gap-2"
              >
                <span className="border-b border-muted-foreground/30 pb-0.5 group-hover:border-accent/50 transition-colors">See how it works</span>
                <motion.span
                  animate={{ y: [0, 3, 0] }}
                  transition={{ repeat: Infinity, duration: 1.5 }}
                  className="text-accent"
                >↓</motion.span>
              </a>
            </motion.div>
          </div>

          {/* Bottom metrics strip */}
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
                  <span className="font-mono text-[8px] text-accent/40 block mb-0.5">{m.num}</span>
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

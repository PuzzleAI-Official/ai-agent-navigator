import { motion, useMotionValue, useTransform, useSpring, useAnimationFrame } from "framer-motion";
import { useEffect, useRef, useState } from "react";

/* ─── Orbital Visualization — the "finding the right AI" concept ─── */
const OrbitalVis = () => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frame = useRef(0);

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

    const cx = w * 0.5;
    const cy = h * 0.48;
    frame.current += 0.003;
    const t = frame.current;

    // Orbital rings
    const rings = [
      { r: 80, dots: 3, speed: 1, opacity: 0.06 },
      { r: 140, dots: 5, speed: -0.7, opacity: 0.05 },
      { r: 210, dots: 8, speed: 0.5, opacity: 0.04 },
      { r: 280, dots: 6, speed: -0.3, opacity: 0.035 },
      { r: 340, dots: 4, speed: 0.2, opacity: 0.025 },
    ];

    rings.forEach((ring) => {
      // Draw ring
      ctx.beginPath();
      ctx.ellipse(cx, cy, ring.r, ring.r * 0.38, -0.15, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(30, 30, 30, ${ring.opacity})`;
      ctx.lineWidth = 0.8;
      ctx.stroke();

      // Draw orbital dots
      for (let i = 0; i < ring.dots; i++) {
        const angle = (i / ring.dots) * Math.PI * 2 + t * ring.speed;
        const x = cx + Math.cos(angle) * ring.r;
        const y = cy + Math.sin(angle) * ring.r * 0.38;
        const z = Math.sin(angle); // depth

        const size = 2 + z * 1.2;
        const alpha = 0.15 + z * 0.15;

        ctx.beginPath();
        ctx.arc(x, y, Math.max(size, 0.5), 0, Math.PI * 2);
        ctx.fillStyle = `rgba(30, 30, 30, ${Math.max(alpha, 0.05)})`;
        ctx.fill();
      }
    });

    // The "chosen" one — accent colored, pulsing
    const chosenAngle = t * 0.5 + 1.2;
    const chosenR = 140;
    const chosenX = cx + Math.cos(chosenAngle) * chosenR;
    const chosenY = cy + Math.sin(chosenAngle) * chosenR * 0.38;

    // Glow
    const gradient = ctx.createRadialGradient(chosenX, chosenY, 0, chosenX, chosenY, 20);
    gradient.addColorStop(0, "rgba(45, 156, 120, 0.25)");
    gradient.addColorStop(1, "rgba(45, 156, 120, 0)");
    ctx.beginPath();
    ctx.arc(chosenX, chosenY, 20, 0, Math.PI * 2);
    ctx.fillStyle = gradient;
    ctx.fill();

    // Core dot
    ctx.beginPath();
    ctx.arc(chosenX, chosenY, 4, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(45, 156, 120, 0.7)";
    ctx.fill();

    // Connection lines from chosen to center
    ctx.beginPath();
    ctx.moveTo(chosenX, chosenY);
    ctx.lineTo(cx, cy);
    ctx.strokeStyle = "rgba(45, 156, 120, 0.08)";
    ctx.lineWidth = 0.8;
    ctx.setLineDash([4, 6]);
    ctx.stroke();
    ctx.setLineDash([]);

    // Center node
    ctx.beginPath();
    ctx.arc(cx, cy, 5, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(30, 30, 30, 0.12)";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx, cy, 2, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(30, 30, 30, 0.3)";
    ctx.fill();

    // Subtle scattered particles
    for (let i = 0; i < 30; i++) {
      const px = cx + Math.cos(i * 2.39996 + t * 0.1) * (100 + i * 12);
      const py = cy + Math.sin(i * 2.39996 + t * 0.08) * (40 + i * 5);
      ctx.beginPath();
      ctx.arc(px, py, 0.8, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(30, 30, 30, ${0.03 + Math.sin(t + i) * 0.02})`;
      ctx.fill();
    }
  });

  return (
    <motion.canvas
      ref={canvasRef}
      className="w-full h-full"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 2, delay: 0.5 }}
      style={{ width: "100%", height: "100%" }}
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
  return (
    <section className="relative min-h-screen flex items-center overflow-hidden">
      {/* Subtle dot grid */}
      <div
        className="absolute inset-0 opacity-[0.3]"
        style={{
          backgroundImage: "radial-gradient(circle at 1px 1px, hsl(var(--foreground) / 0.05) 0.5px, transparent 0)",
          backgroundSize: "28px 28px",
        }}
      />

      {/* Very subtle gradient washes — no image, pure CSS */}
      <div className="absolute top-0 right-0 w-[60%] h-[70%] bg-[radial-gradient(ellipse_at_70%_30%,hsl(160_50%_60%/0.04),transparent_60%)]" />
      <div className="absolute bottom-0 left-[20%] w-[50%] h-[50%] bg-[radial-gradient(ellipse_at_40%_70%,hsl(36_40%_75%/0.06),transparent_60%)]" />

      <div className="max-w-[1400px] mx-auto w-full px-8 relative z-10">
        <div className="grid lg:grid-cols-12 gap-8 items-center min-h-screen py-32">
          {/* Left — Typography */}
          <div className="lg:col-span-6 xl:col-span-5">
            {/* Eyebrow */}
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.3, duration: 0.8 }}
              className="flex items-center gap-4 mb-10"
            >
              <motion.div
                className="w-12 h-px bg-accent"
                initial={{ scaleX: 0 }}
                animate={{ scaleX: 1 }}
                transition={{ delay: 0.5, duration: 0.6 }}
                style={{ transformOrigin: "left" }}
              />
              <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
                The AI hiring platform
              </span>
            </motion.div>

            <motion.h1
              initial={{ opacity: 0, y: 40 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 1, delay: 0.4, ease: [0.23, 1, 0.32, 1] }}
              className="font-display text-[clamp(3rem,7vw,6.5rem)] leading-[0.9] tracking-[-0.03em] text-foreground"
            >
              Find the
              <br />
              <span className="italic text-accent">right</span> AI
              <br />
              <span className="text-muted-foreground/60">for your work.</span>
            </motion.h1>

            <motion.p
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, delay: 0.8 }}
              className="mt-8 text-base md:text-lg text-muted-foreground max-w-md leading-relaxed"
            >
              <TypingText
                text="Describe your workflow. We test 200+ AI solutions against your real use cases — and deliver three verdicts."
                delay={1.2}
              />
            </motion.p>

            {/* CTA */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 1.1 }}
              className="mt-10 flex flex-col sm:flex-row items-start sm:items-center gap-5"
            >
              <a
                href="#start"
                className="group relative inline-flex items-center gap-3 bg-foreground text-background px-7 py-3.5 font-mono text-[12px] uppercase tracking-[0.12em] overflow-hidden transition-all duration-300"
              >
                <span className="relative z-10">Try PuzzleAI</span>
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="relative z-10 transition-transform duration-300 group-hover:translate-x-1">
                  <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
                <div className="absolute inset-0 bg-gradient-to-r from-transparent via-background/10 to-transparent -translate-x-full group-hover:translate-x-full transition-transform duration-700" />
              </a>
              <a
                href="#how-it-works"
                className="font-mono text-[12px] uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors border-b border-muted-foreground/30 pb-0.5"
              >
                See how it works
              </a>
            </motion.div>

            {/* Three metrics */}
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 1.6, duration: 1 }}
              className="mt-16 pt-8 border-t border-border flex gap-10 md:gap-14"
            >
              {[
                { num: "01", label: "Performance" },
                { num: "02", label: "Speed" },
                { num: "03", label: "Cost" },
              ].map((m) => (
                <div key={m.label} className="group cursor-default">
                  <span className="font-mono text-[9px] text-accent/40 block mb-0.5">{m.num}</span>
                  <span className="font-grotesk font-semibold text-xs tracking-tight group-hover:text-accent transition-colors duration-300">{m.label}</span>
                </div>
              ))}
            </motion.div>
          </div>

          {/* Right — Orbital visualization */}
          <div className="lg:col-span-6 xl:col-span-7 h-[500px] lg:h-[600px] relative">
            <OrbitalVis />

            {/* Floating label on the visualization */}
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 2.5, duration: 0.8 }}
              className="absolute bottom-12 right-8 flex items-center gap-2"
            >
              <div className="w-2 h-2 bg-accent/50" />
              <span className="font-mono text-[9px] text-muted-foreground/50 uppercase tracking-[0.2em]">
                200+ solutions mapped
              </span>
            </motion.div>

            {/* Small floating eval card */}
            <motion.div
              initial={{ opacity: 0, scale: 0.9, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              transition={{ delay: 1.8, duration: 0.8, ease: [0.23, 1, 0.32, 1] }}
              className="absolute top-8 right-4 lg:top-12 lg:right-8 w-[200px] border border-border/60 bg-background/80 backdrop-blur-xl p-4 shadow-[0_20px_60px_-15px_hsl(0_0%_0%/0.06)]"
            >
              <div className="flex items-center justify-between mb-3">
                <span className="font-mono text-[7px] uppercase tracking-[0.25em] text-muted-foreground">Top match</span>
                <div className="flex items-center gap-1">
                  <div className="w-1 h-1 bg-accent animate-pulse" />
                  <span className="font-mono text-[7px] text-accent">LIVE</span>
                </div>
              </div>
              <div className="font-grotesk font-medium text-xs mb-1">Claude 3.5 Sonnet</div>
              <div className="h-[2px] bg-muted overflow-hidden mb-2">
                <motion.div
                  className="h-full bg-accent"
                  initial={{ width: 0 }}
                  animate={{ width: "94%" }}
                  transition={{ duration: 1.5, delay: 2.2, ease: "easeOut" }}
                />
              </div>
              <div className="flex justify-between font-mono text-[8px] text-muted-foreground">
                <span>94% match</span>
                <span>0.9s · $0.005</span>
              </div>
            </motion.div>
          </div>
        </div>
      </div>
    </section>
  );
};

export default Hero;

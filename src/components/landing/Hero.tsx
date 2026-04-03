import { motion, useMotionValue, useTransform, useSpring } from "framer-motion";
import { useEffect, useRef, useState, useCallback } from "react";

/* ─── Geometric Depth Composition ─── 
   Overlapping translucent planes, precision grid, architectural annotations.
   Not particles — this is structured, intentional, Swiss-meets-contemporary.
*/

const Hero = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const mouseX = useMotionValue(0.5);
  const mouseY = useMotionValue(0.5);

  const springConfig = { stiffness: 50, damping: 30, mass: 1 };
  const smoothX = useSpring(mouseX, springConfig);
  const smoothY = useSpring(mouseY, springConfig);

  // Parallax transforms for geometric layers
  const layer1X = useTransform(smoothX, [0, 1], [-15, 15]);
  const layer1Y = useTransform(smoothY, [0, 1], [-10, 10]);
  const layer2X = useTransform(smoothX, [0, 1], [20, -20]);
  const layer2Y = useTransform(smoothY, [0, 1], [15, -15]);
  const layer3X = useTransform(smoothX, [0, 1], [-8, 8]);
  const layer3Y = useTransform(smoothY, [0, 1], [-12, 12]);
  const lensX = useTransform(smoothX, [0, 1], [-30, 30]);
  const lensY = useTransform(smoothY, [0, 1], [-20, 20]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    mouseX.set((e.clientX - rect.left) / rect.width);
    mouseY.set((e.clientY - rect.top) / rect.height);
  }, [mouseX, mouseY]);

  // Typing effect
  const [displayed, setDisplayed] = useState("");
  const fullText = "Describe your workflow. We test 200+ AI solutions against your real use cases — and deliver three verdicts.";
  const [started, setStarted] = useState(false);

  useEffect(() => {
    const timeout = setTimeout(() => setStarted(true), 2000);
    return () => clearTimeout(timeout);
  }, []);

  useEffect(() => {
    if (!started) return;
    let i = 0;
    const interval = setInterval(() => {
      setDisplayed(fullText.slice(0, i + 1));
      i++;
      if (i >= fullText.length) clearInterval(interval);
    }, 20);
    return () => clearInterval(interval);
  }, [started]);

  return (
    <section
      ref={containerRef}
      onMouseMove={handleMouseMove}
      className="relative min-h-screen overflow-hidden noise-overlay"
    >
      {/* ── Precision Grid Background ── */}
      <div className="absolute inset-0">
        {/* Main grid */}
        <div
          className="absolute inset-0 opacity-[0.035]"
          style={{
            backgroundImage: `
              linear-gradient(hsl(var(--foreground)) 1px, transparent 1px),
              linear-gradient(90deg, hsl(var(--foreground)) 1px, transparent 1px)
            `,
            backgroundSize: "120px 120px",
          }}
        />
        {/* Fine sub-grid */}
        <div
          className="absolute inset-0 opacity-[0.018]"
          style={{
            backgroundImage: `
              linear-gradient(hsl(var(--foreground)) 0.5px, transparent 0.5px),
              linear-gradient(90deg, hsl(var(--foreground)) 0.5px, transparent 0.5px)
            `,
            backgroundSize: "24px 24px",
          }}
        />
      </div>

      {/* ── Geometric Depth Layers ── */}
      {/* These overlapping translucent shapes create spatial depth */}
      
      {/* Layer 1: Large circle — upper right */}
      <motion.div
        style={{ x: layer1X, y: layer1Y }}
        className="absolute -top-[10%] -right-[5%] w-[55vw] h-[55vw] max-w-[700px] max-h-[700px] pointer-events-none"
      >
        <motion.div
          initial={{ scale: 0.8, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 2, delay: 0.5, ease: [0.16, 1, 0.3, 1] }}
          className="w-full h-full rounded-full"
          style={{
            background: "radial-gradient(circle at 40% 40%, hsl(225 45% 42% / 0.06), hsl(225 45% 42% / 0.01) 60%, transparent 75%)",
            border: "1px solid hsl(225 45% 42% / 0.04)",
          }}
        />
      </motion.div>

      {/* Layer 2: Rectangle plane — mid left */}
      <motion.div
        style={{ x: layer2X, y: layer2Y }}
        className="absolute top-[25%] -left-[8%] w-[40vw] h-[50vh] max-w-[500px] max-h-[500px] pointer-events-none"
      >
        <motion.div
          initial={{ scaleY: 0, opacity: 0 }}
          animate={{ scaleY: 1, opacity: 1 }}
          transition={{ duration: 1.5, delay: 0.8, ease: [0.16, 1, 0.3, 1] }}
          className="w-full h-full origin-top"
          style={{
            background: "linear-gradient(135deg, hsl(30 30% 55% / 0.03), transparent 70%)",
            border: "1px solid hsl(30 30% 55% / 0.03)",
          }}
        />
      </motion.div>

      {/* Layer 3: Small circle — bottom center-right */}
      <motion.div
        style={{ x: layer3X, y: layer3Y }}
        className="absolute bottom-[15%] right-[20%] w-[20vw] h-[20vw] max-w-[280px] max-h-[280px] pointer-events-none"
      >
        <motion.div
          initial={{ scale: 0, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 1.8, delay: 1.2, ease: [0.16, 1, 0.3, 1] }}
          className="w-full h-full rounded-full"
          style={{
            background: "radial-gradient(circle at 50% 50%, hsl(225 45% 42% / 0.04), transparent 65%)",
            border: "1px solid hsl(225 45% 42% / 0.03)",
          }}
        />
      </motion.div>

      {/* ── The "Lens" — a focal evaluation scope ── */}
      <motion.div
        style={{ x: lensX, y: lensY }}
        className="absolute top-[18%] right-[15%] pointer-events-none hidden md:block"
      >
        <motion.div
          initial={{ scale: 0, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 2, delay: 1.5, ease: [0.16, 1, 0.3, 1] }}
          className="relative"
        >
          {/* Outer ring */}
          <svg width="320" height="320" viewBox="0 0 320 320" fill="none" className="opacity-[0.07]">
            <circle cx="160" cy="160" r="155" stroke="hsl(225, 45%, 42%)" strokeWidth="0.5" />
            <circle cx="160" cy="160" r="120" stroke="hsl(225, 45%, 42%)" strokeWidth="0.5" strokeDasharray="4 8" />
            <circle cx="160" cy="160" r="80" stroke="hsl(225, 45%, 42%)" strokeWidth="0.5" />
            {/* Cross-hair */}
            <line x1="160" y1="0" x2="160" y2="320" stroke="hsl(225, 45%, 42%)" strokeWidth="0.5" />
            <line x1="0" y1="160" x2="320" y2="160" stroke="hsl(225, 45%, 42%)" strokeWidth="0.5" />
            {/* Tick marks */}
            {[0, 45, 90, 135, 180, 225, 270, 315].map((angle) => {
              const rad = (angle * Math.PI) / 180;
              const x1 = 160 + Math.cos(rad) * 145;
              const y1 = 160 + Math.sin(rad) * 145;
              const x2 = 160 + Math.cos(rad) * 155;
              const y2 = 160 + Math.sin(rad) * 155;
              return <line key={angle} x1={x1} y1={y1} x2={x2} y2={y2} stroke="hsl(225, 45%, 42%)" strokeWidth="1" />;
            })}
          </svg>

          {/* Rotating inner ring */}
          <motion.div
            className="absolute inset-0 flex items-center justify-center"
            animate={{ rotate: 360 }}
            transition={{ duration: 60, repeat: Infinity, ease: "linear" }}
          >
            <svg width="200" height="200" viewBox="0 0 200 200" fill="none" className="opacity-[0.05]">
              <circle cx="100" cy="100" r="95" stroke="hsl(225, 45%, 42%)" strokeWidth="0.5" strokeDasharray="2 6" />
            </svg>
          </motion.div>

          {/* Center dot */}
          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-2 h-2 bg-accent/20 rounded-full" />
        </motion.div>
      </motion.div>

      {/* ── Architectural Annotations ── */}
      {/* Top-left measurement marks */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 1.8, duration: 1.5 }}
        className="absolute top-10 left-10 pointer-events-none opacity-[0.1] hidden md:block"
      >
        <div className="flex flex-col gap-0">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="flex items-center gap-1.5 h-3">
              <div className={`h-px ${i % 4 === 0 ? 'w-5' : i % 2 === 0 ? 'w-3' : 'w-1.5'} bg-foreground`} />
              {i % 4 === 0 && <span className="font-mono text-[6px] text-foreground">{String(i).padStart(2, '0')}</span>}
            </div>
          ))}
        </div>
      </motion.div>

      {/* Bottom-right coordinates */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 2.2, duration: 1.5 }}
        className="absolute bottom-10 right-10 pointer-events-none opacity-[0.08] hidden md:block"
      >
        <div className="font-mono text-[7px] text-foreground text-right space-y-0.5">
          <div>x: 42.225</div>
          <div>y: 97.180</div>
          <div>z: 01.000</div>
        </div>
      </motion.div>

      {/* Horizontal ruled line */}
      <motion.div
        className="absolute top-[30%] left-0 w-full h-px pointer-events-none"
        initial={{ scaleX: 0, opacity: 0 }}
        animate={{ scaleX: 1, opacity: 0.03 }}
        transition={{ delay: 1, duration: 2, ease: [0.16, 1, 0.3, 1] }}
        style={{ transformOrigin: "left", background: "hsl(var(--foreground))" }}
      />

      {/* Vertical ruled line */}
      <motion.div
        className="absolute top-0 left-[62%] w-px h-full pointer-events-none"
        initial={{ scaleY: 0, opacity: 0 }}
        animate={{ scaleY: 1, opacity: 0.03 }}
        transition={{ delay: 1.3, duration: 2, ease: [0.16, 1, 0.3, 1] }}
        style={{ transformOrigin: "top", background: "hsl(var(--foreground))" }}
      />

      {/* ── Gradient Depth Washes ── */}
      <div className="absolute top-0 left-0 w-[60%] h-[60%] bg-[radial-gradient(ellipse_at_20%_30%,hsl(225_45%_42%/0.03),transparent_60%)] pointer-events-none" />
      <div className="absolute bottom-0 right-0 w-[50%] h-[40%] bg-[radial-gradient(ellipse_at_80%_80%,hsl(30_30%_55%/0.03),transparent_60%)] pointer-events-none" />

      {/* ── Content ── */}
      <div className="relative z-10 min-h-screen flex flex-col justify-between pb-16 md:pb-24">
        <div className="max-w-[1400px] mx-auto w-full px-8 flex-1 flex flex-col justify-center">
          <div className="pt-28 md:pt-0">
            {/* Eyebrow */}
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.3, duration: 0.8 }}
              className="flex items-center gap-4 mb-10"
            >
              <motion.div
                className="w-16 h-px bg-gradient-to-r from-accent to-accent/10"
                initial={{ scaleX: 0 }}
                animate={{ scaleX: 1 }}
                transition={{ delay: 0.5, duration: 1.2 }}
                style={{ transformOrigin: "left" }}
              />
              <span className="font-mono text-[11px] uppercase tracking-[0.3em] text-muted-foreground">
                The AI hiring platform
              </span>
            </motion.div>

            {/* Main headline — massive, architectural */}
            <div className="relative">
              <motion.h1
                initial={{ opacity: 0, y: 80 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 1.4, delay: 0.3, ease: [0.16, 1, 0.3, 1] }}
                className="font-display text-[clamp(4rem,11vw,10rem)] leading-[0.82] tracking-[-0.05em] text-foreground max-w-5xl"
              >
                Find the
                <br />
                <span className="italic text-gradient">right</span> AI.
              </motion.h1>

              {/* Annotation beside headline */}
              <motion.div
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 1.8, duration: 0.8 }}
                className="absolute -right-4 top-4 hidden lg:flex items-start gap-2 opacity-[0.12]"
              >
                <div className="w-12 h-px bg-foreground mt-2" />
                <div className="font-mono text-[7px] text-foreground leading-tight">
                  <div>EVALUATED</div>
                  <div>4,291 TIMES</div>
                </div>
              </motion.div>
            </div>

            {/* Sub copy */}
            <motion.p
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, delay: 0.9 }}
              className="mt-10 text-base md:text-[17px] text-muted-foreground max-w-[440px] leading-[1.7]"
            >
              {displayed}
              {started && displayed.length < fullText.length && (
                <span className="animate-pulse text-accent ml-0.5">|</span>
              )}
            </motion.p>

            {/* CTAs */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 1.2 }}
              className="mt-12 flex flex-col sm:flex-row items-start sm:items-center gap-6"
            >
              <a
                href="#start"
                className="group relative inline-flex items-center gap-3 bg-foreground text-background px-8 py-4 font-mono text-[12px] uppercase tracking-[0.15em] overflow-hidden transition-all duration-500 hover:shadow-[0_20px_60px_-20px_hsl(225_45%_42%/0.35)]"
              >
                <span className="relative z-10">Try PuzzleAI</span>
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="relative z-10 transition-transform duration-300 group-hover:translate-x-1.5">
                  <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
                <div className="absolute inset-0 bg-accent/20 translate-y-full group-hover:translate-y-0 transition-transform duration-500" />
              </a>
              <a
                href="#how-it-works"
                className="group font-mono text-[12px] uppercase tracking-[0.15em] text-muted-foreground hover:text-foreground transition-colors duration-300 flex items-center gap-2"
              >
                <span className="border-b border-muted-foreground/30 pb-0.5 group-hover:border-accent transition-colors duration-300">See how it works</span>
                <motion.span
                  animate={{ y: [0, 4, 0] }}
                  transition={{ repeat: Infinity, duration: 2, ease: "easeInOut" }}
                  className="text-accent/60"
                >↓</motion.span>
              </a>
            </motion.div>
          </div>
        </div>

        {/* ── Bottom metrics strip ── */}
        <div className="max-w-[1400px] mx-auto w-full px-8">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 2.5, duration: 1.2 }}
            className="pt-6 border-t border-border/50 flex flex-wrap items-end justify-between"
          >
            <div className="flex gap-12 md:gap-20">
              {[
                { num: "01", label: "Performance", desc: "use case success rate" },
                { num: "02", label: "Speed", desc: "real-world latency" },
                { num: "03", label: "Cost", desc: "actual token pricing" },
              ].map((m, i) => (
                <motion.div
                  key={m.label}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 2.8 + i * 0.15, duration: 0.5 }}
                  className="group cursor-default"
                >
                  <span className="font-mono text-[8px] text-accent/30 block mb-1">{m.num}</span>
                  <span className="font-grotesk font-semibold text-xs tracking-tight group-hover:text-accent transition-colors duration-500">{m.label}</span>
                  <span className="hidden md:block font-mono text-[9px] text-muted-foreground/35 mt-0.5">{m.desc}</span>
                </motion.div>
              ))}
            </div>

            <motion.div
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: 3, duration: 0.6 }}
              className="hidden md:flex items-center gap-3 border border-border/50 bg-background/60 backdrop-blur-sm px-4 py-2.5"
            >
              <motion.div
                className="w-1.5 h-1.5 bg-accent"
                animate={{ opacity: [0.3, 1, 0.3] }}
                transition={{ repeat: Infinity, duration: 2 }}
              />
              <span className="font-mono text-[9px] text-muted-foreground/60 tracking-wider">4,291 evaluations completed</span>
            </motion.div>
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default Hero;

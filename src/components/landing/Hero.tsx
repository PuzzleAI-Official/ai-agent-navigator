import { motion, useMotionValue, useTransform, useSpring } from "framer-motion";
import { useEffect, useRef, useState } from "react";

/* ─── Generative Puzzle Grid ─── */
const PuzzleGrid = () => {
  const [cells, setCells] = useState<Array<{ id: number; delay: number; color: string }>>([]);
  const cols = 12;
  const rows = 8;

  useEffect(() => {
    const palette = [
      "hsl(160 60% 42% / 0.15)",
      "hsl(160 60% 42% / 0.08)",
      "hsl(240 10% 8% / 0.06)",
      "hsl(36 33% 90% / 0.3)",
      "hsl(160 60% 42% / 0.25)",
      "transparent",
      "transparent",
      "transparent",
    ];
    const grid = Array.from({ length: cols * rows }, (_, i) => ({
      id: i,
      delay: Math.random() * 2,
      color: palette[Math.floor(Math.random() * palette.length)],
    }));
    setCells(grid);
  }, []);

  return (
    <div className="absolute inset-0 grid" style={{ gridTemplateColumns: `repeat(${cols}, 1fr)`, gridTemplateRows: `repeat(${rows}, 1fr)` }}>
      {cells.map((cell) => (
        <motion.div
          key={cell.id}
          initial={{ opacity: 0, scale: 0.3 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.8, delay: cell.delay, ease: [0.23, 1, 0.32, 1] }}
          className="border-[0.5px] border-foreground/[0.04] relative"
          style={{ backgroundColor: cell.color }}
        >
          {/* Inner detail for some cells */}
          {Math.random() > 0.85 && (
            <motion.div
              className="absolute inset-2 border border-accent/20"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: cell.delay + 0.5, duration: 1 }}
            />
          )}
        </motion.div>
      ))}
    </div>
  );
};

/* ─── Floating measurement lines ─── */
const MeasurementLines = () => (
  <div className="absolute inset-0 pointer-events-none overflow-hidden">
    {/* Vertical line */}
    <motion.div
      className="absolute left-[20%] top-0 bottom-0 w-px bg-accent/10"
      initial={{ scaleY: 0 }}
      animate={{ scaleY: 1 }}
      transition={{ duration: 1.5, delay: 1.2, ease: "easeOut" }}
      style={{ transformOrigin: "top" }}
    />
    {/* Horizontal line */}
    <motion.div
      className="absolute top-[60%] left-0 right-0 h-px bg-accent/10"
      initial={{ scaleX: 0 }}
      animate={{ scaleX: 1 }}
      transition={{ duration: 1.5, delay: 1.5, ease: "easeOut" }}
      style={{ transformOrigin: "left" }}
    />
    {/* Coordinate labels */}
    <motion.span
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ delay: 2.2 }}
      className="absolute left-[20%] top-[60%] ml-3 mt-2 font-mono text-[9px] text-accent/30 tracking-widest"
    >
      0.42, 0.60
    </motion.span>
  </div>
);

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
    }, 30);
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
  const mouseX = useMotionValue(0);
  const mouseY = useMotionValue(0);
  const springX = useSpring(mouseX, { stiffness: 50, damping: 20 });
  const springY = useSpring(mouseY, { stiffness: 50, damping: 20 });
  const rotateX = useTransform(springY, [-0.5, 0.5], [2, -2]);
  const rotateY = useTransform(springX, [-0.5, 0.5], [-2, 2]);

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    mouseX.set((e.clientX - rect.left) / rect.width - 0.5);
    mouseY.set((e.clientY - rect.top) / rect.height - 0.5);
  };

  return (
    <section
      ref={containerRef}
      onMouseMove={handleMouseMove}
      className="relative min-h-screen flex items-end pb-20 md:pb-32 overflow-hidden"
    >
      {/* Generative background */}
      <div className="absolute inset-0">
        <PuzzleGrid />
        <MeasurementLines />
        {/* Radial gradient overlay */}
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,hsl(160_60%_42%/0.08),transparent)]" />
        <div className="absolute inset-0 bg-gradient-to-b from-background/60 via-background/20 to-background" />
      </div>

      {/* Floating 3D card — the "puzzle piece" */}
      <motion.div
        style={{ rotateX, rotateY, perspective: 1000 }}
        className="absolute top-28 right-8 md:right-24 w-[280px] md:w-[360px] hidden md:block"
      >
        <motion.div
          initial={{ opacity: 0, y: 60, rotateZ: 3 }}
          animate={{ opacity: 1, y: 0, rotateZ: 0 }}
          transition={{ duration: 1, delay: 0.8, ease: [0.23, 1, 0.32, 1] }}
          className="border border-border bg-background/80 backdrop-blur-sm p-8 shadow-[0_40px_100px_-20px_hsl(160_60%_42%/0.15)]"
        >
          <div className="font-mono text-[9px] uppercase tracking-[0.2em] text-muted-foreground mb-6">
            Evaluation #4,291
          </div>
          
          {/* Mini result preview */}
          <div className="space-y-4 mb-6">
            {[
              { name: "Claude 3.5 Sonnet", score: 94, bar: "w-[94%]" },
              { name: "GPT-4o", score: 89, bar: "w-[89%]" },
              { name: "Gemini Pro 1.5", score: 76, bar: "w-[76%]" },
            ].map((item, i) => (
              <div key={item.name}>
                <div className="flex justify-between mb-1.5">
                  <span className="font-mono text-[10px] text-foreground/70">{item.name}</span>
                  <span className="font-display text-sm">{item.score}%</span>
                </div>
                <div className="h-[3px] bg-muted overflow-hidden">
                  <motion.div
                    className="h-full bg-accent"
                    initial={{ width: 0 }}
                    animate={{ width: `${item.score}%` }}
                    transition={{ duration: 1.2, delay: 1.5 + i * 0.2, ease: "easeOut" }}
                  />
                </div>
              </div>
            ))}
          </div>

          <div className="flex items-center gap-2 pt-4 border-t border-border">
            <div className="w-1.5 h-1.5 bg-accent animate-pulse" />
            <span className="font-mono text-[9px] text-accent tracking-wider">LIVE — testing in progress</span>
          </div>
        </motion.div>
      </motion.div>

      {/* Main content */}
      <div className="max-w-[1400px] mx-auto w-full px-8 relative z-10">
        {/* Eyebrow with animated line */}
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
            transition={{ delay: 0.5, duration: 0.6 }}
            style={{ transformOrigin: "left" }}
          />
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
            The AI hiring platform
          </span>
        </motion.div>

        <motion.h1
          initial={{ opacity: 0, y: 50 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 1, delay: 0.4, ease: [0.23, 1, 0.32, 1] }}
          className="font-display text-[clamp(3.2rem,9vw,8.5rem)] leading-[0.9] tracking-[-0.03em] text-foreground max-w-5xl"
        >
          Find the{" "}
          <span className="relative inline-block">
            <span className="italic">right</span>
            <motion.svg
              viewBox="0 0 120 50"
              className="absolute -inset-x-3 -inset-y-2 w-[calc(100%+24px)] h-[calc(100%+16px)]"
              fill="none"
              initial={{ pathLength: 0 }}
            >
              <ellipse
                cx="60" cy="25" rx="55" ry="20"
                stroke="hsl(160 60% 42%)"
                strokeWidth="1.5"
                className="draw-circle"
                transform="rotate(-2 60 25)"
              />
            </motion.svg>
          </span>{" "}
          AI
          <br />
          <span className="text-muted-foreground">for your work.</span>
        </motion.h1>

        <motion.p
          initial={{ opacity: 0, y: 30 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.8 }}
          className="mt-8 text-lg md:text-xl text-muted-foreground max-w-xl leading-relaxed font-light"
        >
          <TypingText
            text="Describe your workflow. We test 200+ AI solutions against your real use cases — and deliver three verdicts."
            delay={1.2}
          />
        </motion.p>

        {/* CTA row */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 1.1 }}
          className="mt-10 flex flex-col sm:flex-row items-start sm:items-center gap-6"
        >
          <a
            href="#start"
            className="group inline-flex items-center gap-3 bg-foreground text-background px-8 py-4 font-mono text-[13px] uppercase tracking-[0.12em] hover:gap-5 transition-all duration-300"
          >
            Try PuzzleAI
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="transition-transform duration-300 group-hover:translate-x-1">
              <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </a>
          <a
            href="#how-it-works"
            className="font-mono text-[13px] uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors border-b border-muted-foreground/30 pb-0.5"
          >
            See how it works
          </a>
        </motion.div>

        {/* Bottom metric strip */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 1.8, duration: 1 }}
          className="mt-20 pt-8 border-t border-border flex flex-wrap gap-12 md:gap-20"
        >
          {[
            { value: "Performance", desc: "How many use cases succeed" },
            { value: "Speed", desc: "Real latency, not benchmarks" },
            { value: "Cost", desc: "Actual pricing on your workload" },
          ].map((m, i) => (
            <div key={m.value} className="group cursor-default">
              <div className="flex items-center gap-3 mb-1">
                <span className="font-mono text-[10px] text-muted-foreground/50">0{i + 1}</span>
                <span className="font-grotesk font-semibold text-sm tracking-tight">{m.value}</span>
              </div>
              <p className="font-mono text-[10px] text-muted-foreground/60 tracking-wide group-hover:text-muted-foreground transition-colors">{m.desc}</p>
            </div>
          ))}
        </motion.div>
      </div>
    </section>
  );
};

export default Hero;

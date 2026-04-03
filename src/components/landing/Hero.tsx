import { motion, useMotionValue, useTransform, useSpring } from "framer-motion";
import { useEffect, useRef, useState } from "react";

/* ─── Organic flowing lines — like topographic contours ─── */
const FlowField = () => {
  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none">
      <svg
        viewBox="0 0 1400 900"
        className="absolute inset-0 w-full h-full"
        fill="none"
        preserveAspectRatio="xMidYMid slice"
      >
        {/* Flowing contour lines */}
        {[...Array(12)].map((_, i) => {
          const y = 100 + i * 65;
          const opacity = i < 3 ? 0.03 + i * 0.01 : i > 9 ? 0.06 - (i - 9) * 0.015 : 0.05;
          return (
            <motion.path
              key={i}
              d={`M-50 ${y} C300 ${y - 30 + Math.sin(i) * 40} 600 ${y + 20 + Math.cos(i) * 50} 900 ${y - 10 + Math.sin(i * 0.7) * 30} S1200 ${y + 15 + Math.cos(i * 1.3) * 25} 1450 ${y}`}
              stroke="hsl(160 60% 42%)"
              strokeWidth="1"
              opacity={opacity}
              initial={{ pathLength: 0, opacity: 0 }}
              animate={{ pathLength: 1, opacity }}
              transition={{ duration: 2.5, delay: i * 0.12, ease: "easeOut" }}
            />
          );
        })}
        
        {/* Accent intersection dots */}
        {[
          { cx: 340, cy: 280 }, { cx: 720, cy: 420 }, { cx: 1050, cy: 340 },
          { cx: 200, cy: 500 }, { cx: 890, cy: 220 },
        ].map((dot, i) => (
          <motion.circle
            key={i}
            cx={dot.cx}
            cy={dot.cy}
            r="2"
            fill="hsl(160 60% 42%)"
            initial={{ opacity: 0, scale: 0 }}
            animate={{ opacity: 0.3, scale: 1 }}
            transition={{ delay: 2 + i * 0.2, duration: 0.5 }}
          />
        ))}
      </svg>

      {/* Soft radial glow */}
      <div className="absolute top-0 right-0 w-[70%] h-[80%] bg-[radial-gradient(ellipse_at_70%_30%,hsl(160_60%_42%/0.06),transparent_70%)]" />
      <div className="absolute bottom-0 left-0 w-[50%] h-[60%] bg-[radial-gradient(ellipse_at_20%_80%,hsl(36_33%_90%/0.5),transparent_70%)]" />
    </div>
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
    }, 25);
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
  const springX = useSpring(mouseX, { stiffness: 40, damping: 25 });
  const springY = useSpring(mouseY, { stiffness: 40, damping: 25 });
  const cardRotateX = useTransform(springY, [-0.5, 0.5], [3, -3]);
  const cardRotateY = useTransform(springX, [-0.5, 0.5], [-3, 3]);

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
      <FlowField />

      {/* Floating 3D evaluation card */}
      <motion.div
        style={{ rotateX: cardRotateX, rotateY: cardRotateY, perspective: 1200 }}
        className="absolute top-32 right-8 md:right-20 lg:right-32 w-[260px] md:w-[320px] hidden md:block"
      >
        <motion.div
          initial={{ opacity: 0, y: 80, rotateZ: 2 }}
          animate={{ opacity: 1, y: 0, rotateZ: 0 }}
          transition={{ duration: 1.2, delay: 1, ease: [0.23, 1, 0.32, 1] }}
          className="border border-border/60 bg-background/70 backdrop-blur-xl p-7 shadow-[0_60px_120px_-30px_hsl(160_60%_42%/0.12)]"
        >
          <div className="flex items-center justify-between mb-6">
            <div className="font-mono text-[8px] uppercase tracking-[0.25em] text-muted-foreground">
              Eval #4,291
            </div>
            <div className="flex items-center gap-1.5">
              <div className="w-1 h-1 bg-accent animate-pulse" />
              <span className="font-mono text-[8px] text-accent tracking-wider">LIVE</span>
            </div>
          </div>
          
          <div className="space-y-4 mb-5">
            {[
              { name: "Claude 3.5", score: 94 },
              { name: "GPT-4o", score: 89 },
              { name: "Gemini 1.5", score: 76 },
            ].map((item, i) => (
              <div key={item.name}>
                <div className="flex justify-between mb-1">
                  <span className="font-mono text-[10px] text-foreground/60">{item.name}</span>
                  <span className="font-display text-sm italic">{item.score}%</span>
                </div>
                <div className="h-[2px] bg-muted overflow-hidden">
                  <motion.div
                    className="h-full bg-accent"
                    initial={{ width: 0 }}
                    animate={{ width: `${item.score}%` }}
                    transition={{ duration: 1.4, delay: 1.8 + i * 0.25, ease: "easeOut" }}
                  />
                </div>
              </div>
            ))}
          </div>

          <div className="pt-4 border-t border-border/60 grid grid-cols-3 gap-3">
            {[
              { label: "Perf", val: "94%" },
              { label: "Speed", val: "0.9s" },
              { label: "Cost", val: "$0.005" },
            ].map((m) => (
              <div key={m.label} className="text-center">
                <div className="font-display text-lg">{m.val}</div>
                <div className="font-mono text-[7px] uppercase tracking-[0.2em] text-muted-foreground">{m.label}</div>
              </div>
            ))}
          </div>
        </motion.div>
      </motion.div>

      {/* Main content */}
      <div className="max-w-[1400px] mx-auto w-full px-8 relative z-10">
        {/* Eyebrow */}
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
          className="font-display text-[clamp(3.2rem,9vw,8.5rem)] leading-[0.88] tracking-[-0.03em] text-foreground max-w-5xl"
        >
          Find the{" "}
          <span className="relative inline-block">
            <span className="italic">right</span>
            <motion.svg
              viewBox="0 0 120 50"
              className="absolute -inset-x-3 -inset-y-2 w-[calc(100%+24px)] h-[calc(100%+16px)]"
              fill="none"
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

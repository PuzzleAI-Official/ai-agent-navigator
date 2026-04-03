import { motion, useMotionValue, useTransform, useSpring } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import heroBg from "@/assets/hero-bg.jpg";

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
  const bgX = useTransform(springX, [-0.5, 0.5], [10, -10]);
  const bgY = useTransform(springY, [-0.5, 0.5], [10, -10]);

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
      {/* Parallax background image */}
      <motion.div
        style={{ x: bgX, y: bgY }}
        className="absolute inset-[-20px]"
      >
        <img
          src={heroBg}
          alt=""
          width={1920}
          height={1080}
          className="w-full h-full object-cover opacity-40"
        />
      </motion.div>

      {/* Dot grid overlay */}
      <div
        className="absolute inset-0 opacity-[0.35]"
        style={{
          backgroundImage: "radial-gradient(circle at 1px 1px, hsl(var(--foreground) / 0.07) 1px, transparent 0)",
          backgroundSize: "32px 32px",
        }}
      />

      {/* Gradient overlays for depth */}
      <div className="absolute inset-0 bg-gradient-to-b from-background/40 via-transparent to-background/90" />
      <div className="absolute inset-0 bg-gradient-to-r from-background/60 via-transparent to-transparent" />

      {/* Decorative measurement lines */}
      <motion.div
        className="absolute top-[15%] right-[18%] w-px h-[300px] bg-gradient-to-b from-transparent via-accent/15 to-transparent"
        initial={{ scaleY: 0 }}
        animate={{ scaleY: 1 }}
        transition={{ duration: 2, delay: 0.8 }}
        style={{ transformOrigin: "top" }}
      />
      <motion.div
        className="absolute top-[55%] left-[10%] w-[200px] h-px bg-gradient-to-r from-transparent via-accent/15 to-transparent"
        initial={{ scaleX: 0 }}
        animate={{ scaleX: 1 }}
        transition={{ duration: 1.5, delay: 1.2 }}
        style={{ transformOrigin: "left" }}
      />

      {/* Small floating coordinate labels */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 2.5, duration: 1 }}
        className="absolute top-[14%] right-[17%] font-mono text-[8px] text-accent/25 tracking-[0.3em]"
      >
        42.7°N
      </motion.div>

      {/* Floating 3D evaluation card */}
      <motion.div
        style={{ rotateX: cardRotateX, rotateY: cardRotateY, perspective: 1200 }}
        className="absolute top-28 right-8 md:right-16 lg:right-28 w-[260px] md:w-[300px] hidden lg:block"
      >
        <motion.div
          initial={{ opacity: 0, y: 80, rotateZ: 2 }}
          animate={{ opacity: 1, y: 0, rotateZ: 0 }}
          transition={{ duration: 1.2, delay: 0.8, ease: [0.23, 1, 0.32, 1] }}
          className="border border-border bg-background/80 backdrop-blur-xl p-6 shadow-[0_60px_120px_-30px_hsl(160_60%_42%/0.1),0_20px_40px_-10px_hsl(0_0%_0%/0.05)]"
        >
          <div className="flex items-center justify-between mb-5">
            <div className="font-mono text-[8px] uppercase tracking-[0.25em] text-muted-foreground">
              Eval #4,291
            </div>
            <div className="flex items-center gap-1.5">
              <div className="w-1 h-1 bg-accent animate-pulse" />
              <span className="font-mono text-[8px] text-accent tracking-wider">LIVE</span>
            </div>
          </div>
          
          <div className="space-y-3.5 mb-5">
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

          <div className="pt-4 border-t border-border grid grid-cols-3 gap-3">
            {[
              { label: "Perf", val: "94%" },
              { label: "Speed", val: "0.9s" },
              { label: "Cost", val: "$0.005" },
            ].map((m) => (
              <div key={m.label} className="text-center">
                <div className="font-display text-base">{m.val}</div>
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
            <svg
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
            </svg>
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
            className="group relative inline-flex items-center gap-3 bg-foreground text-background px-8 py-4 font-mono text-[13px] uppercase tracking-[0.12em] overflow-hidden transition-all duration-300"
          >
            <span className="relative z-10">Try PuzzleAI</span>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="relative z-10 transition-transform duration-300 group-hover:translate-x-1">
              <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            {/* Hover shimmer */}
            <div className="absolute inset-0 bg-gradient-to-r from-transparent via-background/10 to-transparent -translate-x-full group-hover:translate-x-full transition-transform duration-700" />
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
                <span className="font-mono text-[10px] text-accent/40">0{i + 1}</span>
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

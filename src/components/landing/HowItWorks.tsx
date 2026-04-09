import { motion } from "framer-motion";
import { useState } from "react";

/* ─── Step visuals ─── */

const DescribeVisual = () => (
  <div className="space-y-2.5">
    {[
      { text: "Summarize contracts under 2 pages", delay: 0.6 },
      { text: "Extract key dates and parties", delay: 0.9 },
      { text: "Flag unusual vs standard clauses", delay: 1.2 },
    ].map((line, i) => (
      <motion.div
        key={i}
        initial={{ opacity: 0, x: -12, scale: 0.95 }}
        whileInView={{ opacity: 1, x: 0, scale: 1 }}
        viewport={{ once: true }}
        transition={{ delay: line.delay, duration: 0.4, ease: [0.23, 1, 0.32, 1] }}
        className="flex items-center gap-3 bg-secondary/60 border border-border/60 px-3.5 py-2.5"
      >
        <div className="w-4 h-4 border border-accent/30 flex items-center justify-center flex-shrink-0" style={{ transform: "rotate(45deg)" }}>
          <motion.div
            className="w-2 h-2 bg-accent"
            initial={{ scale: 0 }}
            whileInView={{ scale: 1 }}
            viewport={{ once: true }}
            transition={{ delay: line.delay + 0.3 }}
          />
        </div>
        <span className="font-mono text-[10px] text-foreground/60 leading-tight">{line.text}</span>
      </motion.div>
    ))}
    <motion.div
      initial={{ opacity: 0 }}
      whileInView={{ opacity: 1 }}
      viewport={{ once: true }}
      transition={{ delay: 1.6 }}
      className="flex items-center gap-2 pl-1 pt-1"
    >
      <span className="w-1 h-3 bg-accent/50 animate-pulse" />
      <span className="font-mono text-[9px] text-muted-foreground/40">analyzing requirements...</span>
    </motion.div>
  </div>
);

const TestVisual = () => {
  const providers = [
    { name: "Mindee V2", progress: 94, delay: 0.6, duration: 1.8 },
    { name: "Verify OCR", progress: 89, delay: 0.8, duration: 2.0 },
    { name: "Nanonets ITM", progress: 76, delay: 1.0, duration: 2.2 },
    { name: "Klippa", progress: 68, delay: 1.2, duration: 2.4 },
  ];

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between mb-1">
        <span className="font-mono text-[8px] uppercase tracking-[0.2em] text-muted-foreground/50">RUNNING 5 TEST CASES</span>
        <motion.span
          animate={{ opacity: [0.3, 1, 0.3] }}
          transition={{ repeat: Infinity, duration: 1.5 }}
          className="font-mono text-[8px] text-accent"
        >● live</motion.span>
      </div>
      {providers.map((p) => (
        <div key={p.name}>
          <div className="flex justify-between mb-1">
            <span className="font-mono text-[10px] text-foreground/50">{p.name}</span>
            <motion.span
              className="font-display text-xs italic text-foreground/70"
              initial={{ opacity: 0 }}
              whileInView={{ opacity: 1 }}
              viewport={{ once: true }}
              transition={{ delay: p.delay + p.duration }}
            >{p.progress}%</motion.span>
          </div>
          <div className="h-[3px] bg-muted/60 overflow-hidden">
            <motion.div
              className="h-full bg-gradient-to-r from-accent/60 to-accent"
              initial={{ width: 0 }}
              whileInView={{ width: `${p.progress}%` }}
              viewport={{ once: true }}
              transition={{ delay: p.delay, duration: p.duration, ease: "easeOut" }}
            />
          </div>
        </div>
      ))}
    </div>
  );
};

const DecideVisual = () => (
  <div className="space-y-2">
    {[
      { metric: "Performance", value: "94%", icon: "◈" },
      { metric: "Speed", value: "0.9s", icon: "◈" },
      { metric: "Cost", value: "$0.003", icon: "◈" },
    ].map((m, i) => (
      <motion.div
        key={m.metric}
        initial={{ opacity: 0, y: 8 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        transition={{ delay: 0.8 + i * 0.15 }}
        className="flex items-center justify-between border border-border/60 px-3.5 py-2.5 bg-secondary/30 hover:bg-accent/[0.04] transition-colors duration-300"
      >
        <div className="flex items-center gap-2.5">
          <span className="text-accent/40 text-xs">{m.icon}</span>
          <span className="font-mono text-[10px] uppercase tracking-wider text-foreground/50">{m.metric}</span>
        </div>
        <span className="font-display text-base italic">{m.value}</span>
      </motion.div>
    ))}
  </div>
);

const steps = [
  {
    num: "01",
    title: "Describe",
    headline: "Tell us what you need AI to do.",
    body: "In plain language, describe your workflow, upload sample data, or paste your current process. Our system extracts testable requirements — no technical setup needed.",
    visual: <DescribeVisual />,
  },
  {
    num: "02",
    title: "Test",
    headline: "We run every candidate against your reality.",
    body: "We match relevant AI solutions from indexed provider database, synthesize comprehensive test data, and run each candidate head-to-head on your actual scenarios.",
    visual: <TestVisual />,
  },
  {
    num: "03",
    title: "Decide",
    headline: "Your numbers. No noise.",
    body: "Customizable metrics that surface the numbers that actually matter for your workflow — nothing more, nothing less.",
    visual: <DecideVisual />,
  },
];

const HowItWorks = () => {
  const [hoveredStep, setHoveredStep] = useState<number | null>(null);

  return (
    <section id="how-it-works" className="py-32 relative">
      {/* Unique diagonal grid pattern — not dot grid */}
      <div
        className="absolute inset-0 opacity-[0.025]"
        style={{
          backgroundImage: `repeating-linear-gradient(
            -45deg,
            transparent,
            transparent 80px,
            hsl(var(--foreground)) 80px,
            hsl(var(--foreground)) 81px
          )`,
        }}
      />

      {/* Offset vertical accent — asymmetric placement */}
      <div className="absolute top-0 left-[15%] w-px h-full bg-gradient-to-b from-transparent via-accent/[0.08] to-transparent pointer-events-none" />

      <div className="max-w-[1400px] mx-auto px-8 relative">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="mb-24"
        >
          {/* Section label — stacked instead of inline */}
          <div className="mb-8">
            <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-accent/50 block">
              How it works
            </span>
            <div className="w-8 h-[2px] bg-accent/30 mt-3" style={{ transform: "skewX(-20deg)" }} />
          </div>
          <h2 className="font-display text-[clamp(2.5rem,5vw,4.5rem)] leading-[1] tracking-[-0.02em] max-w-2xl">
            From confusion to
            <br />
            <span className="italic text-gradient">clarity</span> in minutes.
          </h2>
        </motion.div>

        <div className="space-y-0">
          {steps.map((step, i) => (
            <motion.div
              key={step.num}
              initial={{ opacity: 0, y: 40 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-80px" }}
              transition={{ duration: 0.6, delay: i * 0.1 }}
              className="border-t border-border group cursor-default relative"
              onMouseEnter={() => setHoveredStep(i)}
              onMouseLeave={() => setHoveredStep(null)}
            >
              {/* Diagonal accent bar on hover — signature interaction */}
              <div className="absolute top-0 left-0 w-0 h-[2px] bg-accent group-hover:w-[30%] transition-all duration-700" style={{ transform: "skewX(-20deg)", transformOrigin: "left" }} />

              <div className="grid md:grid-cols-12 gap-8 items-start py-16 md:py-20">
                <div className="md:col-span-1">
                  <span className="font-grotesk font-bold text-[13px] text-accent/25">{step.num}</span>
                </div>
                <div className="md:col-span-2">
                  <h3 className="font-display text-4xl md:text-5xl tracking-[-0.02em] group-hover:translate-x-2 transition-transform duration-500">
                    {step.title}
                    <motion.span
                      className="inline-block w-1.5 h-1.5 bg-accent ml-2 align-super"
                      style={{ transform: "rotate(45deg)" }}
                      initial={{ scale: 0 }}
                      animate={{ scale: hoveredStep === i ? 1 : 0 }}
                      transition={{ duration: 0.2 }}
                    />
                  </h3>
                </div>
                <div className="md:col-span-5">
                  <p className="font-grotesk text-xl md:text-2xl font-medium text-foreground leading-snug mb-4">
                    {step.headline}
                  </p>
                  <p className="text-muted-foreground leading-relaxed max-w-xl text-[15px]">
                    {step.body}
                  </p>
                </div>
                <div className="md:col-span-4">
                  <motion.div
                    animate={{
                      opacity: hoveredStep === i ? 1 : 0.5,
                      y: hoveredStep === i ? 0 : 6,
                    }}
                    transition={{ duration: 0.4 }}
                    className="p-5 border border-border/60 bg-background/80 backdrop-blur-sm shadow-[0_8px_30px_-12px_hsl(0_0%_0%/0.04)]"
                  >
                    {step.visual}
                  </motion.div>
                </div>
              </div>
            </motion.div>
          ))}
          <div className="border-t border-border" />
        </div>
      </div>
    </section>
  );
};

export default HowItWorks;

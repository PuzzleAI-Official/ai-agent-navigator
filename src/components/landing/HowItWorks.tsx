import { motion } from "framer-motion";
import { useState } from "react";

const steps = [
  {
    num: "01",
    title: "Describe",
    headline: "Tell us what you need AI to do.",
    body: "In plain language, describe your workflow, upload sample data, or paste your current process. Our system extracts testable requirements — no technical setup needed.",
    visual: (
      <div className="space-y-3">
        {["Summarize legal contracts under 2 pages", "Extract key dates and parties", "Flag unusual clauses vs. standard templates"].map((line, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, x: -10 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true }}
            transition={{ delay: 0.8 + i * 0.15 }}
            className="flex items-start gap-3"
          >
            <div className="w-1.5 h-1.5 bg-accent mt-1.5 flex-shrink-0" />
            <span className="font-mono text-[11px] text-foreground/60 leading-relaxed">{line}</span>
          </motion.div>
        ))}
      </div>
    ),
  },
  {
    num: "02",
    title: "Test",
    headline: "We run every candidate against your reality.",
    body: "We match relevant AI solutions from 200+ indexed providers, synthesize comprehensive test data, and run each candidate head-to-head on your actual scenarios.",
    visual: (
      <div className="grid grid-cols-4 gap-1.5">
        {Array.from({ length: 16 }, (_, i) => {
          const isActive = [0, 2, 5, 7, 8, 10, 13, 15].includes(i);
          return (
            <motion.div
              key={i}
              initial={{ opacity: 0, scale: 0.5 }}
              whileInView={{ opacity: 1, scale: 1 }}
              viewport={{ once: true }}
              transition={{ delay: 0.8 + i * 0.04 }}
              className={`aspect-square border ${
                isActive
                  ? "border-accent/30 bg-accent/10"
                  : "border-border bg-secondary/30"
              } flex items-center justify-center`}
            >
              {isActive && <div className="w-1 h-1 bg-accent" />}
            </motion.div>
          );
        })}
      </div>
    ),
  },
  {
    num: "03",
    title: "Decide",
    headline: "Three numbers. No noise.",
    body: "Performance — how many use cases each solution handles. Speed — real latency. Cost — actual pricing on your workload. You decide.",
    visual: (
      <div className="space-y-4">
        {[
          { label: "Performance", value: "94%", bar: 94 },
          { label: "Speed", value: "0.9s", bar: 82 },
          { label: "Cost", value: "$0.003", bar: 70 },
        ].map((m, i) => (
          <div key={m.label}>
            <div className="flex justify-between mb-1.5">
              <span className="font-mono text-[10px] text-muted-foreground uppercase tracking-wider">{m.label}</span>
              <span className="font-display text-sm italic">{m.value}</span>
            </div>
            <div className="h-[3px] bg-muted overflow-hidden">
              <motion.div
                className="h-full bg-accent"
                initial={{ width: 0 }}
                whileInView={{ width: `${m.bar}%` }}
                viewport={{ once: true }}
                transition={{ duration: 1, delay: 0.8 + i * 0.2 }}
              />
            </div>
          </div>
        ))}
      </div>
    ),
  },
];

const HowItWorks = () => {
  const [hoveredStep, setHoveredStep] = useState<number | null>(null);

  return (
    <section id="how-it-works" className="py-32 relative">
      {/* Subtle background texture */}
      <div
        className="absolute inset-0 opacity-[0.25]"
        style={{
          backgroundImage: "radial-gradient(circle at 1px 1px, hsl(var(--foreground) / 0.04) 1px, transparent 0)",
          backgroundSize: "48px 48px",
        }}
      />

      <div className="max-w-[1400px] mx-auto px-8 relative">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="mb-24"
        >
          <div className="flex items-center gap-4 mb-6">
            <motion.div
              className="w-8 h-px bg-accent"
              initial={{ scaleX: 0 }}
              whileInView={{ scaleX: 1 }}
              viewport={{ once: true }}
              transition={{ duration: 0.6 }}
              style={{ transformOrigin: "left" }}
            />
            <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
              How it works
            </span>
          </div>
          <h2 className="font-display text-[clamp(2.5rem,5vw,4.5rem)] leading-[1] tracking-[-0.02em] max-w-2xl">
            From confusion to
            <br />
            <span className="italic">clarity</span> in minutes.
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
              className="border-t border-border group cursor-default"
              onMouseEnter={() => setHoveredStep(i)}
              onMouseLeave={() => setHoveredStep(null)}
            >
              <div className="grid md:grid-cols-12 gap-8 items-start py-16 md:py-20">
                <div className="md:col-span-1">
                  <span className="font-mono text-[11px] text-accent/40">{step.num}</span>
                </div>
                <div className="md:col-span-2">
                  <h3 className="font-display text-4xl md:text-5xl tracking-[-0.02em] group-hover:translate-x-2 transition-transform duration-500">
                    {step.title}
                    <motion.span
                      className="inline-block w-2 h-2 bg-accent ml-2 align-super"
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
                      opacity: hoveredStep === i ? 1 : 0.4,
                      y: hoveredStep === i ? 0 : 4,
                    }}
                    transition={{ duration: 0.4 }}
                    className="p-6 border border-border bg-card/50 backdrop-blur-sm shadow-sm"
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

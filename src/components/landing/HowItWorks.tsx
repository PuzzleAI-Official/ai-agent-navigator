import { motion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";

const steps = [
  {
    num: "01",
    title: "Describe",
    headline: "Tell us what you need AI to do.",
    body: "In plain language, describe your workflow, upload sample data, or paste your current process. Our system extracts testable requirements from your description — no technical setup needed.",
  },
  {
    num: "02",
    title: "Test",
    headline: "We run every candidate against your reality.",
    body: "We match relevant AI solutions from 200+ indexed providers, synthesize comprehensive test data that covers edge cases you haven't thought of, and run each candidate head-to-head on your actual scenarios.",
  },
  {
    num: "03",
    title: "Decide",
    headline: "Three numbers. No noise.",
    body: "Performance — how many of your use cases each solution actually handles. Speed — real latency, not benchmarks. Cost — actual token pricing on your workload. That's it. You decide.",
  },
];

const HowItWorks = () => {
  const containerRef = useRef<HTMLDivElement>(null);

  return (
    <section id="how-it-works" className="py-32" ref={containerRef}>
      <div className="max-w-[1400px] mx-auto px-8">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="mb-24"
        >
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground block mb-6">
            How it works
          </span>
          <h2 className="font-display text-[clamp(2.5rem,5vw,4.5rem)] leading-[1] tracking-[-0.02em] max-w-2xl">
            From confusion to
            <br />
            <span className="italic">clarity</span> in minutes.
          </h2>
        </motion.div>

        {/* Editorial steps — full width, stacked */}
        <div className="space-y-0">
          {steps.map((step, i) => (
            <motion.div
              key={step.num}
              initial={{ opacity: 0, y: 40 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-80px" }}
              transition={{ duration: 0.6, delay: i * 0.1 }}
              className="border-t border-border py-16 md:py-20 group cursor-default"
            >
              <div className="grid md:grid-cols-12 gap-8 items-start">
                {/* Number */}
                <div className="md:col-span-1">
                  <span className="font-mono text-[11px] text-muted-foreground">{step.num}</span>
                </div>
                {/* Title */}
                <div className="md:col-span-3">
                  <h3 className="font-display text-4xl md:text-5xl tracking-[-0.02em] group-hover:translate-x-2 transition-transform duration-500">
                    {step.title}
                  </h3>
                </div>
                {/* Content */}
                <div className="md:col-span-8">
                  <p className="font-grotesk text-xl md:text-2xl font-medium text-foreground leading-snug mb-4">
                    {step.headline}
                  </p>
                  <p className="text-muted-foreground leading-relaxed max-w-xl">
                    {step.body}
                  </p>
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

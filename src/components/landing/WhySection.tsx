import { motion } from "framer-motion";
import { Zap, DollarSign, Target, Clock, Shield, Users } from "lucide-react";
import { useState } from "react";

const reasons = [
  {
    icon: Target,
    title: "Real testing, not reviews",
    desc: "We don't aggregate opinions. We run your actual use cases against every solution and measure what passes.",
    stat: "94%",
    statLabel: "avg test coverage",
  },
  {
    icon: Clock,
    title: "Minutes, not weeks",
    desc: "Stop manually evaluating 15 tools. Describe what you need once, get results in under 5 minutes.",
    stat: "<5min",
    statLabel: "to first result",
  },
  {
    icon: DollarSign,
    title: "True cost visibility",
    desc: "We calculate actual token costs and API pricing based on your workload — not theoretical benchmarks.",
    stat: "3x",
    statLabel: "more cost clarity",
  },
  {
    icon: Zap,
    title: "Built for SMBs",
    desc: "You don't have a research team. We are your research team. Simple input, decisive output.",
    stat: "200+",
    statLabel: "AI solutions indexed",
  },
];

const WhySection = () => {
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  return (
    <section id="why-puzzleai" className="py-32 relative overflow-hidden">
      {/* Background accent */}
      <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-border to-transparent" />

      <div className="container max-w-5xl mx-auto px-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.5 }}
          className="text-center mb-16"
        >
          <span className="font-mono text-xs text-primary tracking-widest uppercase">Why PuzzleAI</span>
          <h2 className="text-4xl md:text-5xl font-heading font-bold mt-3 tracking-tight">
            The AI selection problem,{" "}
            <span className="text-muted-foreground">solved.</span>
          </h2>
        </motion.div>

        <div className="grid sm:grid-cols-2 gap-5">
          {reasons.map((r, i) => (
            <motion.div
              key={r.title}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.4, delay: i * 0.1 }}
              onMouseEnter={() => setHoveredIdx(i)}
              onMouseLeave={() => setHoveredIdx(null)}
              className="relative rounded-2xl border border-border bg-card p-7 transition-all duration-300 hover:shadow-xl hover:border-primary/20 border-gradient-card cursor-default group"
            >
              <div className="flex items-start justify-between mb-4">
                <div className="h-11 w-11 rounded-xl bg-accent flex items-center justify-center group-hover:scale-110 transition-transform duration-300">
                  <r.icon className="h-5 w-5 text-accent-foreground" />
                </div>

                <motion.div
                  initial={false}
                  animate={{ opacity: hoveredIdx === i ? 1 : 0.5, scale: hoveredIdx === i ? 1.05 : 1 }}
                  className="text-right"
                >
                  <div className="text-2xl font-heading font-bold text-gradient">{r.stat}</div>
                  <div className="text-[10px] font-mono text-muted-foreground">{r.statLabel}</div>
                </motion.div>
              </div>

              <h3 className="font-heading font-semibold text-lg text-foreground mb-2">{r.title}</h3>
              <p className="text-sm text-muted-foreground leading-relaxed">{r.desc}</p>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
};

export default WhySection;

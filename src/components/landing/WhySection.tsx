import { motion } from "framer-motion";
import { Zap, DollarSign, Target, Clock } from "lucide-react";

const reasons = [
  {
    icon: Target,
    title: "Real testing, not reviews",
    desc: "We don't aggregate opinions. We run your actual use cases against every solution and measure what passes.",
  },
  {
    icon: Clock,
    title: "Minutes, not weeks",
    desc: "Stop manually evaluating 15 tools. Describe what you need once, get results in under 5 minutes.",
  },
  {
    icon: DollarSign,
    title: "True cost visibility",
    desc: "We calculate actual token costs and API pricing based on your workload — not theoretical benchmarks.",
  },
  {
    icon: Zap,
    title: "Built for SMBs",
    desc: "You don't have a research team. We are your research team. Simple input, decisive output.",
  },
];

const WhySection = () => {
  return (
    <section id="why" className="py-32 relative">
      {/* Divider line */}
      <div className="absolute top-0 left-1/2 -translate-x-1/2 w-px h-24 bg-gradient-to-b from-transparent via-border to-transparent" />

      <div className="container max-w-5xl mx-auto px-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.5 }}
          className="mb-16"
        >
          <span className="font-mono text-xs text-primary tracking-widest uppercase">Why PuzzleAI</span>
          <h2 className="text-4xl md:text-5xl font-heading font-bold mt-3 tracking-tight">
            The AI selection problem,
            <br />
            <span className="text-muted-foreground">solved.</span>
          </h2>
        </motion.div>

        <div className="grid sm:grid-cols-2 gap-6">
          {reasons.map((r, i) => (
            <motion.div
              key={r.title}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.5, delay: i * 0.1 }}
              className="flex gap-4 p-6 rounded-xl border border-border bg-card hover:border-primary/20 transition-colors"
            >
              <div className="h-10 w-10 shrink-0 rounded-lg bg-secondary flex items-center justify-center">
                <r.icon className="h-5 w-5 text-primary" />
              </div>
              <div>
                <h3 className="font-heading font-semibold text-foreground mb-1">{r.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">{r.desc}</p>
              </div>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
};

export default WhySection;

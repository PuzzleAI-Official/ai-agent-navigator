import { motion } from "framer-motion";
import { MessageSquare, FlaskConical, BarChart3 } from "lucide-react";

const steps = [
  {
    number: "01",
    icon: MessageSquare,
    title: "Describe your workflow",
    description:
      "Tell us in natural language what you need AI to do. Upload your current workflow, sample data, or just explain your use case.",
  },
  {
    number: "02",
    icon: FlaskConical,
    title: "We test everything",
    description:
      "We match relevant AI solutions, synthesize comprehensive test data from your inputs, and run every candidate against your real scenarios.",
  },
  {
    number: "03",
    icon: BarChart3,
    title: "Three numbers. Your decision.",
    description:
      "Performance (how many test cases pass), speed, and cost. No fluff metrics. Just what you need to choose with confidence.",
  },
];

const HowItWorks = () => {
  return (
    <section id="how-it-works" className="py-32 relative">
      <div className="container max-w-5xl mx-auto px-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.5 }}
          className="mb-16"
        >
          <span className="font-mono text-xs text-primary tracking-widest uppercase">Process</span>
          <h2 className="text-4xl md:text-5xl font-heading font-bold mt-3 tracking-tight">
            How it works
          </h2>
        </motion.div>

        <div className="grid md:grid-cols-3 gap-6">
          {steps.map((step, i) => (
            <motion.div
              key={step.number}
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.5, delay: i * 0.15 }}
              className="relative group"
            >
              <div className="rounded-xl border border-border bg-card p-8 h-full transition-colors hover:border-primary/30">
                <span className="font-mono text-xs text-muted-foreground">{step.number}</span>
                <div className="mt-4 mb-4 h-10 w-10 rounded-lg bg-secondary flex items-center justify-center">
                  <step.icon className="h-5 w-5 text-primary" />
                </div>
                <h3 className="text-xl font-heading font-semibold mb-3 text-foreground">{step.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">{step.description}</p>
              </div>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
};

export default HowItWorks;

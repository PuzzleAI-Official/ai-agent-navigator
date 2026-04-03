import { motion } from "framer-motion";
import { MessageSquare, FlaskConical, BarChart3, Upload, Cpu, CheckCircle2 } from "lucide-react";
import { useState } from "react";

const steps = [
  {
    number: "01",
    icon: MessageSquare,
    title: "Describe your workflow",
    description: "Tell us in natural language what you need AI to do. Upload your current workflow, sample data, or just explain your use case.",
    detail: "Our AI understands complex business contexts and extracts testable requirements from your description.",
    visual: [
      { icon: Upload, label: "Upload workflow docs" },
      { icon: MessageSquare, label: "Describe in plain English" },
    ],
  },
  {
    number: "02",
    icon: FlaskConical,
    title: "We test everything",
    description: "We match relevant AI solutions, synthesize comprehensive test data from your inputs, and run every candidate against your real scenarios.",
    detail: "Test data is generated to cover edge cases you might not have considered — filling gaps in your sample data.",
    visual: [
      { icon: Cpu, label: "200+ AI solutions scanned" },
      { icon: FlaskConical, label: "Automated test generation" },
    ],
  },
  {
    number: "03",
    icon: BarChart3,
    title: "Three numbers. You decide.",
    description: "Performance (how many test cases pass), speed, and cost. No fluff metrics. Just what you need to choose with confidence.",
    detail: "We calculate real token costs and measure actual latency — not synthetic benchmarks.",
    visual: [
      { icon: CheckCircle2, label: "Performance score" },
      { icon: BarChart3, label: "Speed & cost analysis" },
    ],
  },
];

const HowItWorks = () => {
  const [activeStep, setActiveStep] = useState(0);

  return (
    <section id="how-it-works" className="py-32 relative">
      <div className="container max-w-5xl mx-auto px-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.5 }}
          className="text-center mb-16"
        >
          <span className="font-mono text-xs text-primary tracking-widest uppercase">Process</span>
          <h2 className="text-4xl md:text-5xl font-heading font-bold mt-3 tracking-tight">
            Three steps to clarity
          </h2>
          <p className="text-muted-foreground mt-4 max-w-lg mx-auto">
            From confusion to confidence in under five minutes.
          </p>
        </motion.div>

        {/* Step selector tabs */}
        <div className="flex justify-center gap-2 mb-12">
          {steps.map((step, i) => (
            <button
              key={step.number}
              onClick={() => setActiveStep(i)}
              className={`
                relative px-6 py-3 rounded-xl text-sm font-medium transition-all duration-300 cursor-pointer
                ${activeStep === i
                  ? "bg-card border border-primary/20 text-foreground shadow-lg glow-subtle"
                  : "text-muted-foreground hover:text-foreground hover:bg-secondary"
                }
              `}
            >
              <span className="font-mono text-xs mr-2 text-primary">{step.number}</span>
              <span className="hidden sm:inline">{step.title}</span>
              {activeStep === i && (
                <motion.div
                  layoutId="step-indicator"
                  className="absolute -bottom-px left-4 right-4 h-0.5 bg-gradient-brand rounded-full"
                />
              )}
            </button>
          ))}
        </div>

        {/* Active step content */}
        <motion.div
          key={activeStep}
          initial={{ opacity: 0, y: 15 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
          className="rounded-2xl border border-border bg-card p-8 md:p-12 border-gradient-card"
        >
          <div className="grid md:grid-cols-2 gap-8 items-center">
            <div>
              <div className="h-12 w-12 rounded-xl bg-accent flex items-center justify-center mb-5">
                {(() => {
                  const Icon = steps[activeStep].icon;
                  return <Icon className="h-6 w-6 text-accent-foreground" />;
                })()}
              </div>
              <h3 className="text-2xl font-heading font-bold mb-3 text-foreground">
                {steps[activeStep].title}
              </h3>
              <p className="text-muted-foreground leading-relaxed mb-4">
                {steps[activeStep].description}
              </p>
              <p className="text-sm text-muted-foreground/80 leading-relaxed">
                {steps[activeStep].detail}
              </p>
            </div>

            {/* Visual indicators */}
            <div className="space-y-4">
              {steps[activeStep].visual.map((v, i) => (
                <motion.div
                  key={v.label}
                  initial={{ opacity: 0, x: 20 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: i * 0.15, duration: 0.4 }}
                  className="flex items-center gap-4 p-4 rounded-xl border border-border bg-secondary/40 hover:bg-secondary/70 transition-colors group cursor-default"
                >
                  <div className="h-10 w-10 rounded-lg bg-accent flex items-center justify-center shrink-0 group-hover:scale-105 transition-transform">
                    <v.icon className="h-5 w-5 text-accent-foreground" />
                  </div>
                  <span className="text-sm font-medium text-foreground">{v.label}</span>
                </motion.div>
              ))}
            </div>
          </div>
        </motion.div>
      </div>
    </section>
  );
};

export default HowItWorks;

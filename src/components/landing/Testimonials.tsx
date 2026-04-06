import { motion } from "framer-motion";
import { useNavigate } from "react-router-dom";
import { FileText, Inbox, Send, Phone, MessageCircle, Code } from "lucide-react";

const useCases = [
  {
    title: "Document Parsing",
    desc: "Extract, summarize, and structure data from contracts, invoices, and reports.",
    Icon: FileText,
    color: "hsl(220, 70%, 55%)",
    bgColor: "hsl(220, 70%, 55%, 0.08)",
  },
  {
    title: "Inbound Agents",
    desc: "AI agents that handle customer inquiries, support tickets, and lead qualification.",
    Icon: Inbox,
    color: "hsl(150, 60%, 40%)",
    bgColor: "hsl(150, 60%, 40%, 0.08)",
  },
  {
    title: "Outbound Agents",
    desc: "Automate outreach, follow-ups, and personalized messaging at scale.",
    Icon: Send,
    color: "hsl(280, 60%, 55%)",
    bgColor: "hsl(280, 60%, 55%, 0.08)",
  },
  {
    title: "Voice & Phone Agents",
    desc: "Real-time AI voice assistants for calls, IVR, and conversational workflows.",
    Icon: Phone,
    color: "hsl(340, 65%, 50%)",
    bgColor: "hsl(340, 65%, 50%, 0.08)",
  },
  {
    title: "Chatbot Agents",
    desc: "Deploy intelligent chatbots across web, Slack, and messaging platforms.",
    Icon: MessageCircle,
    color: "hsl(30, 80%, 50%)",
    bgColor: "hsl(30, 80%, 50%, 0.08)",
  },
  {
    title: "Code Generation",
    desc: "AI-powered code writing, review, and refactoring for engineering teams.",
    Icon: Code,
    color: "hsl(190, 70%, 45%)",
    bgColor: "hsl(190, 70%, 45%, 0.08)",
  },
];

const Testimonials = () => {
  const navigate = useNavigate();

  return (
    <section id="companies" className="py-32 relative">
      <div className="absolute inset-0 bg-gradient-to-b from-background via-card/20 to-background" />

      <div className="max-w-[1400px] mx-auto px-8 relative">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
          className="mb-12"
        >
          <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-accent/50 block">
            Trending
          </span>
          <div className="w-8 h-[2px] bg-accent/30 mt-3 mb-6" style={{ transform: "skewX(-20deg)" }} />
          <h2 className="font-display text-[clamp(2rem,4vw,3.5rem)] leading-[1.05] tracking-[-0.02em]">
            Find agents that help with
          </h2>
        </motion.div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {useCases.map((item, i) => (
            <motion.div
              key={item.title}
              initial={{ opacity: 0, y: 24 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.4, delay: i * 0.07 }}
              onClick={() => navigate("/playground")}
              className="group border border-border p-7 cursor-pointer hover:border-accent/30 transition-all duration-500 bg-background/50 backdrop-blur-sm relative overflow-hidden"
            >
              {/* Ghost icon on hover */}
              <div
                className="absolute -top-4 -right-4 opacity-0 group-hover:opacity-100 transition-all duration-700 pointer-events-none"
              >
                <item.Icon
                  size={96}
                  strokeWidth={1}
                  style={{ color: item.color, opacity: 0.12 }}
                  className="group-hover:scale-110 transition-transform duration-700"
                />
              </div>

              <div className="absolute bottom-0 left-0 w-0 h-[2px] group-hover:w-[40%] transition-all duration-700" style={{ transform: "skewX(-20deg)", backgroundColor: item.color, opacity: 0.5 }} />

              <h3 className="font-grotesk font-semibold text-[15px] group-hover:translate-x-1 transition-transform duration-500 mb-3 relative z-10">
                {item.title}
              </h3>
              <p className="text-muted-foreground text-sm leading-relaxed relative z-10">
                {item.desc}
              </p>
              <span className="absolute top-6 right-6 font-grotesk text-[10px] text-accent/0 group-hover:text-accent/40 transition-colors duration-500 uppercase tracking-wider z-10">
                Try →
              </span>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
};

export default Testimonials;

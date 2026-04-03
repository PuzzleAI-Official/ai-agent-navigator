import { motion } from "framer-motion";
import { useNavigate } from "react-router-dom";

const useCases = [
  {
    title: "Document Parsing",
    desc: "Extract, summarize, and structure data from contracts, invoices, and reports.",
    icon: "◈",
    providers: [
      { name: "Anthropic", logo: "https://cdn.brandfetch.io/anthropic.com/w/512/h/512/logo" },
      { name: "OpenAI", logo: "https://cdn.brandfetch.io/openai.com/w/512/h/512/logo" },
      { name: "Google", logo: "https://cdn.brandfetch.io/google.com/w/512/h/512/logo" },
    ],
  },
  {
    title: "Inbound Agents",
    desc: "AI agents that handle customer inquiries, support tickets, and lead qualification.",
    icon: "◇",
    providers: [
      { name: "Intercom", logo: "https://cdn.brandfetch.io/intercom.com/w/512/h/512/logo" },
      { name: "Zendesk", logo: "https://cdn.brandfetch.io/zendesk.com/w/512/h/512/logo" },
      { name: "Ada", logo: "https://cdn.brandfetch.io/ada.cx/w/512/h/512/logo" },
    ],
  },
  {
    title: "Outbound Agents",
    desc: "Automate outreach, follow-ups, and personalized messaging at scale.",
    icon: "▹",
    providers: [
      { name: "Apollo", logo: "https://cdn.brandfetch.io/apollo.io/w/512/h/512/logo" },
      { name: "Outreach", logo: "https://cdn.brandfetch.io/outreach.io/w/512/h/512/logo" },
      { name: "Salesloft", logo: "https://cdn.brandfetch.io/salesloft.com/w/512/h/512/logo" },
    ],
  },
  {
    title: "Voice & Phone Agents",
    desc: "Real-time AI voice assistants for calls, IVR, and conversational workflows.",
    icon: "◎",
    providers: [
      { name: "Vapi", logo: "https://cdn.brandfetch.io/vapi.ai/w/512/h/512/logo" },
      { name: "Bland AI", logo: "https://cdn.brandfetch.io/bland.ai/w/512/h/512/logo" },
      { name: "Retell", logo: "https://cdn.brandfetch.io/retellai.com/w/512/h/512/logo" },
    ],
  },
  {
    title: "Chatbot Agents",
    desc: "Deploy intelligent chatbots across web, Slack, and messaging platforms.",
    icon: "⬡",
    providers: [
      { name: "Botpress", logo: "https://cdn.brandfetch.io/botpress.com/w/512/h/512/logo" },
      { name: "Voiceflow", logo: "https://cdn.brandfetch.io/voiceflow.com/w/512/h/512/logo" },
      { name: "Rasa", logo: "https://cdn.brandfetch.io/rasa.com/w/512/h/512/logo" },
    ],
  },
  {
    title: "Code Generation",
    desc: "AI-powered code writing, review, and refactoring for engineering teams.",
    icon: "⟐",
    providers: [
      { name: "Cursor", logo: "https://cdn.brandfetch.io/cursor.com/w/512/h/512/logo" },
      { name: "GitHub", logo: "https://cdn.brandfetch.io/github.com/w/512/h/512/logo" },
      { name: "Codeium", logo: "https://cdn.brandfetch.io/codeium.com/w/512/h/512/logo" },
    ],
  },
];

const ProviderLogos = ({ providers }: { providers: typeof useCases[0]["providers"] }) => (
  <div className="absolute -top-1 -right-1 pointer-events-none opacity-0 group-hover:opacity-100 transition-all duration-600">
    {providers.map((p, i) => (
      <img
        key={p.name}
        src={p.logo}
        alt={p.name}
        className="absolute object-contain transition-all duration-700 opacity-0 group-hover:opacity-[0.12] grayscale"
        style={{
          width: `${40 - i * 6}px`,
          height: `${40 - i * 6}px`,
          top: `${i * 28 + 12}px`,
          right: `${i * 20 + 16}px`,
          transitionDelay: `${i * 100}ms`,
          transform: `rotate(${-3 + i * 4}deg)`,
        }}
        onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
      />
    ))}
  </div>
);

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
            Discover Use Cases
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
              <ProviderLogos providers={item.providers} />
              <div className="absolute bottom-0 left-0 w-0 h-[2px] bg-accent/40 group-hover:w-[40%] transition-all duration-700" style={{ transform: "skewX(-20deg)" }} />
              <div className="flex items-start gap-3 mb-3 relative z-10">
                <span className="text-accent/40 text-sm mt-0.5">{item.icon}</span>
                <h3 className="font-grotesk font-semibold text-[15px] group-hover:translate-x-1 transition-transform duration-500">
                  {item.title}
                </h3>
              </div>
              <p className="text-muted-foreground text-sm leading-relaxed pl-[1.6rem] relative z-10">
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

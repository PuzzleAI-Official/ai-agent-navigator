import { motion } from "framer-motion";
import { useNavigate } from "react-router-dom";

const useCases = [
  {
    title: "Document Parsing",
    desc: "Extract, summarize, and structure data from contracts, invoices, and reports.",
    icon: "◈",
    providers: [
      { name: "Anthropic", logo: "https://logo.clearbit.com/anthropic.com" },
      { name: "OpenAI", logo: "https://logo.clearbit.com/openai.com" },
      { name: "Google", logo: "https://logo.clearbit.com/google.com" },
    ],
  },
  {
    title: "Inbound Agents",
    desc: "AI agents that handle customer inquiries, support tickets, and lead qualification.",
    icon: "◇",
    providers: [
      { name: "Intercom", logo: "https://logo.clearbit.com/intercom.com" },
      { name: "Zendesk", logo: "https://logo.clearbit.com/zendesk.com" },
      { name: "Ada", logo: "https://logo.clearbit.com/ada.cx" },
    ],
  },
  {
    title: "Outbound Agents",
    desc: "Automate outreach, follow-ups, and personalized messaging at scale.",
    icon: "▹",
    providers: [
      { name: "Apollo", logo: "https://logo.clearbit.com/apollo.io" },
      { name: "Outreach", logo: "https://logo.clearbit.com/outreach.io" },
      { name: "Salesloft", logo: "https://logo.clearbit.com/salesloft.com" },
    ],
  },
  {
    title: "Voice & Phone Agents",
    desc: "Real-time AI voice assistants for calls, IVR, and conversational workflows.",
    icon: "◎",
    providers: [
      { name: "Vapi", logo: "https://logo.clearbit.com/vapi.ai" },
      { name: "Twilio", logo: "https://logo.clearbit.com/twilio.com" },
      { name: "Vonage", logo: "https://logo.clearbit.com/vonage.com" },
    ],
  },
  {
    title: "Chatbot Agents",
    desc: "Deploy intelligent chatbots across web, Slack, and messaging platforms.",
    icon: "⬡",
    providers: [
      { name: "Botpress", logo: "https://logo.clearbit.com/botpress.com" },
      { name: "Voiceflow", logo: "https://logo.clearbit.com/voiceflow.com" },
      { name: "Rasa", logo: "https://logo.clearbit.com/rasa.com" },
    ],
  },
  {
    title: "Code Generation",
    desc: "AI-powered code writing, review, and refactoring for engineering teams.",
    icon: "⟐",
    providers: [
      { name: "Cursor", logo: "https://logo.clearbit.com/cursor.com" },
      { name: "GitHub", logo: "https://logo.clearbit.com/github.com" },
      { name: "Codeium", logo: "https://logo.clearbit.com/codeium.com" },
    ],
  },
];

const ProviderLogos = ({ providers }: { providers: typeof useCases[0]["providers"] }) => (
  <div className="absolute top-0 right-0 w-[55%] h-full pointer-events-none">
    {providers.map((p, i) => (
      <motion.img
        key={p.name}
        src={p.logo}
        alt={p.name}
        initial={{ opacity: 0, scale: 0.7, y: 10 }}
        className="absolute object-contain grayscale"
        style={{
          width: `${36 - i * 4}px`,
          height: `${36 - i * 4}px`,
          top: `${16 + i * 30}px`,
          right: `${12 + i * 24}px`,
          opacity: 0,
        }}
        onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
      />
    ))}
    {/* Fallback: always show text names on hover */}
    {providers.map((p, i) => (
      <span
        key={`text-${p.name}`}
        className="absolute font-grotesk font-black uppercase tracking-[-0.02em] text-foreground/0 group-hover:text-foreground/[0.06] transition-all duration-700 select-none whitespace-nowrap"
        style={{
          fontSize: `${26 - i * 5}px`,
          top: `${14 + i * 30}px`,
          right: `${10 + i * 18}px`,
          transform: `rotate(${-3 + i * 3}deg)`,
          transitionDelay: `${i * 80}ms`,
        }}
      >
        {p.name}
      </span>
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

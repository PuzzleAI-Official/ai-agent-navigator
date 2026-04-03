import { motion } from "framer-motion";
import { useNavigate } from "react-router-dom";

import anthropicLogo from "@/assets/providers/anthropic.png";
import openaiLogo from "@/assets/providers/openai.png";
import googleLogo from "@/assets/providers/google.png";
import intercomLogo from "@/assets/providers/intercom.png";
import zendeskLogo from "@/assets/providers/zendesk.png";
import adaLogo from "@/assets/providers/ada.png";
import apolloLogo from "@/assets/providers/apollo.png";
import outreachLogo from "@/assets/providers/outreach.png";
import salesloftLogo from "@/assets/providers/salesloft.png";
import vapiLogo from "@/assets/providers/vapi.png";
import twilioLogo from "@/assets/providers/twilio.png";
import vonageLogo from "@/assets/providers/vonage.png";
import botpressLogo from "@/assets/providers/botpress.png";
import voiceflowLogo from "@/assets/providers/voiceflow.png";
import rasaLogo from "@/assets/providers/rasa.png";
import cursorLogo from "@/assets/providers/cursor.png";
import githubLogo from "@/assets/providers/github.png";
import codeiumLogo from "@/assets/providers/codeium.png";

const useCases = [
  {
    title: "Document Parsing",
    desc: "Extract, summarize, and structure data from contracts, invoices, and reports.",
    icon: "◈",
    providers: [
      { name: "Anthropic", logo: anthropicLogo },
      { name: "OpenAI", logo: openaiLogo },
      { name: "Google", logo: googleLogo },
    ],
  },
  {
    title: "Inbound Agents",
    desc: "AI agents that handle customer inquiries, support tickets, and lead qualification.",
    icon: "◇",
    providers: [
      { name: "Intercom", logo: intercomLogo },
      { name: "Zendesk", logo: zendeskLogo },
      { name: "Ada", logo: adaLogo },
    ],
  },
  {
    title: "Outbound Agents",
    desc: "Automate outreach, follow-ups, and personalized messaging at scale.",
    icon: "▹",
    providers: [
      { name: "Apollo", logo: apolloLogo },
      { name: "Outreach", logo: outreachLogo },
      { name: "Salesloft", logo: salesloftLogo },
    ],
  },
  {
    title: "Voice & Phone Agents",
    desc: "Real-time AI voice assistants for calls, IVR, and conversational workflows.",
    icon: "◎",
    providers: [
      { name: "Vapi", logo: vapiLogo },
      { name: "Twilio", logo: twilioLogo },
      { name: "Vonage", logo: vonageLogo },
    ],
  },
  {
    title: "Chatbot Agents",
    desc: "Deploy intelligent chatbots across web, Slack, and messaging platforms.",
    icon: "⬡",
    providers: [
      { name: "Botpress", logo: botpressLogo },
      { name: "Voiceflow", logo: voiceflowLogo },
      { name: "Rasa", logo: rasaLogo },
    ],
  },
  {
    title: "Code Generation",
    desc: "AI-powered code writing, review, and refactoring for engineering teams.",
    icon: "⟐",
    providers: [
      { name: "Cursor", logo: cursorLogo },
      { name: "GitHub", logo: githubLogo },
      { name: "Codeium", logo: codeiumLogo },
    ],
  },
];

const ProviderLogos = ({ providers }: { providers: typeof useCases[0]["providers"] }) => (
  <div className="absolute top-0 right-0 w-[50%] h-full pointer-events-none flex items-start justify-end gap-2 p-4 pt-5 pr-5">
    {providers.map((p, i) => (
      <img
        key={p.name}
        src={p.logo}
        alt={p.name}
        loading="lazy"
        width={512}
        height={512}
        className="opacity-0 group-hover:opacity-[0.10] transition-all duration-700 grayscale object-contain"
        style={{
          width: `${32 - i * 4}px`,
          height: `${32 - i * 4}px`,
          transitionDelay: `${i * 100}ms`,
          transform: `translateY(${i * 6}px)`,
        }}
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

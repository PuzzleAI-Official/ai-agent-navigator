import { motion } from "framer-motion";
import { useState } from "react";
import buddyproLogo from "@/assets/buddypro-logo.png";
import elevenlabsLogo from "@/assets/elevenlabs-logo.svg";
import geminiLogo from "@/assets/gemini-logo.svg";
import hermesLogo from "@/assets/hermes-logo.png";
import llamaindexLogo from "@/assets/llamaindex-logo.png";
import openaiLogo from "@/assets/openai-logo.svg";
import manusLogo from "@/assets/manus-logo.png";
import mindeeLogo from "@/assets/mindee-logo.png";
import nanonetsLogo from "@/assets/nanonets-logo.png";
import openclawLogo from "@/assets/openclaw-logo.png";
import reductoLogo from "@/assets/reducto-logo.png";
import synthflowLogo from "@/assets/synthflow-logo.png";
import vapiLogo from "@/assets/vapi-logo.png";
import vellumLogo from "@/assets/vellum-logo.png";
import windsurfLogo from "@/assets/windsurf-logo.png";

type Agent = {
  rank: number;
  name: string;
  provider: string;
  note: string;
  logo?: string;
};

const CATEGORIES: { key: string; title: string; blurb: string; agents: Agent[] }[] = [
  {
    key: "productivity",
    title: "Productivity",
    blurb: "Document parsing, summarization, research, and knowledge work.",
    agents: [
      {
        rank: 1,
        name: "Reducto",
        provider: "Reducto AI",
        note: "High-accuracy document ingestion and parsing for AI pipelines",
        logo: reductoLogo,
      },
      {
        rank: 2,
        name: "LlamaIndex",
        provider: "LlamaIndex",
        note: "Data framework for building LLM-powered agents over your data",
        logo: llamaindexLogo,
      },
      {
        rank: 3,
        name: "Nanonets",
        provider: "Nanonets",
        note: "AI document parsing and data extraction for enterprise workflows",
        logo: nanonetsLogo,
      },
      {
        rank: 4,
        name: "Mindee",
        provider: "Mindee",
        note: "API for structured document data extraction",
        logo: mindeeLogo,
      },
      {
        rank: 5,
        name: "Windsurf (Coming soon)",
        provider: "Windsurf",
        note: "AI coding workspace and app-building assistant",
        logo: windsurfLogo,
      },
    ],
  },
  {
    key: "voice_agent",
    title: "Voice Agent",
    blurb: "Phone, IVR, and low-latency conversational flows.",
    agents: [
      {
        rank: 1,
        name: "GPT-Realtime",
        provider: "OpenAI",
        note: "Native speech-to-speech",
        logo: openaiLogo,
      },
      {
        rank: 2,
        name: "ElevenLabs",
        provider: "ElevenLabs",
        note: "Ultra-realistic TTS voices",
        logo: elevenlabsLogo,
      },
      {
        rank: 3,
        name: "Gemini 3.1 Flash TTS",
        provider: "Google",
        note: "Cheap, fast multilingual TTS",
        logo: geminiLogo,
      },
      {
        rank: 4,
        name: "Vapi (Coming soon)",
        provider: "Vapi",
        note: "Telephony-grade voice agents",
        logo: vapiLogo,
      },
      {
        rank: 5,
        name: "Synthflow (Coming soon)",
        provider: "Synthflow",
        note: "No-code voice agent builder",
        logo: synthflowLogo,
      },
    ],
  },
  {
    key: "personal_agent",
    title: "Personal Agent",
    blurb: "Character agents, story generation, conversational play.",
    agents: [
      {
        rank: 1,
        name: "Hermes Agent",
        provider: "Nous Research",
        note: "Open-source reasoning agent",
        logo: hermesLogo,
      },
      {
        rank: 2,
        name: "OpenClaw",
        provider: "OpenClaw",
        note: "Autonomous agent that can execute tasks via LLMs",
        logo: openclawLogo,
      },
      {
        rank: 3,
        name: "BuddyPro AI (Coming soon)",
        provider: "BuddyPro",
        note: "Monetizable AI digital twins from expert knowledge",
        logo: buddyproLogo,
      },
      {
        rank: 4,
        name: "Vellum (Coming soon)",
        provider: "Vellum",
        note: "Prompt engineering, semantic search, and agent orchestration",
        logo: vellumLogo,
      },
      {
        rank: 5,
        name: "Manus (Coming soon)",
        provider: "Butterfly Effect",
        note: "General-purpose autonomous AI agent for complex tasks",
        logo: manusLogo,
      },
    ],
  },
  {
    key: "entertainment",
    title: "Entertainment",
    blurb: "Phone agents, IVR, low-latency conversational flows.",
    agents: [],
  },
];

const ProviderLogo = ({ src, alt }: { src?: string; alt: string }) => {
  const [failed, setFailed] = useState(false);

  if (!src || failed) {
    return (
      <span className="inline-flex items-center justify-center w-[18px] h-[18px] bg-accent/10 text-[10px] font-grotesk font-semibold text-accent/80 shrink-0">
        {alt.charAt(0)}
      </span>
    );
  }

  return (
    <img
      src={src}
      alt={`${alt} logo`}
      width={18}
      height={18}
      loading="lazy"
      onError={() => setFailed(true)}
      className="w-[18px] h-[18px] object-contain shrink-0"
    />
  );
};

type Category = (typeof CATEGORIES)[number];

const CategoryBlock = ({ cat, index }: { cat: Category; index: number }) => {
  const [expanded, setExpanded] = useState(false);

  return (
    <motion.div
      initial={{ opacity: 0, y: 24 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true }}
      transition={{ duration: 0.4, delay: index * 0.05 }}
    >
      <div className="border-b border-border pb-3 mb-5">
        <h3 className="font-display text-[1.5rem] tracking-[-0.01em]">{cat.title}</h3>
      </div>

      {cat.agents.length === 0 ? (
        <div className="py-8 text-center font-grotesk text-[13px] uppercase tracking-[0.2em] text-muted-foreground border-b border-border/40">
          Coming soon
        </div>
      ) : (
        <ol className="space-y-3">
          {cat.agents.map((agent) => (
            <li
              key={agent.name}
              className="group grid grid-cols-[28px_1fr] items-baseline gap-4 py-2 border-b border-border/40 hover:border-accent/40 transition-colors"
            >
              <span className="font-mono text-[11px] text-accent/60 tabular-nums">
                {String(agent.rank).padStart(2, "0")}
              </span>
              <div>
                <div className="flex items-center gap-2 font-grotesk font-semibold text-[14px] text-foreground">
                  <ProviderLogo src={agent.logo} alt={agent.provider} />
                  <span>{agent.name}</span>
                </div>
                <div className="text-[12px] text-muted-foreground mt-0.5 pl-[26px]">
                  {agent.provider} - {agent.note}
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}

      {expanded && (
        <div className="mt-4 py-4 px-5 border border-dashed border-border/60 text-[12px] text-muted-foreground leading-[1.7] font-grotesk">
          More agents in this category are being benchmarked. New entries are added weekly as evaluations complete.
        </div>
      )}

      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="mt-6 inline-flex items-center gap-2.5 px-5 py-2.5 border border-border/60 font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-foreground/70 hover:text-foreground hover:border-foreground/30 transition-colors"
        aria-expanded={expanded}
      >
        <span>{expanded ? "Show less" : "Show more"}</span>
        <span
          className="inline-block transition-transform duration-300"
          style={{ transform: expanded ? "rotate(180deg)" : "rotate(0deg)" }}
        >
          v
        </span>
      </button>
    </motion.div>
  );
};

const AgentRoutingDirectory = () => {
  return (
    <section className="mt-32">
      <div className="flex items-center gap-4 mb-8">
        <div className="w-8 h-[2px] bg-accent/30" style={{ transform: "skewX(-20deg)" }} />
        <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-muted-foreground">
          Routing Directory
        </span>
      </div>

      <h2 className="font-display text-[clamp(2rem,4vw,3.5rem)] leading-[1.05] tracking-[-0.02em] mb-6">
        Agents All-In-One API
      </h2>
      <p className="text-[15px] md:text-[17px] text-muted-foreground max-w-[680px] leading-[1.8] mb-16">
        The Puzzle API routes requests across the agent landscape. Below are some top agents per category. New agents
        and models are added weekly.
      </p>

      <div className="grid md:grid-cols-2 gap-x-12 gap-y-16">
        {CATEGORIES.map((cat, index) => (
          <CategoryBlock key={cat.key} cat={cat} index={index} />
        ))}
      </div>
    </section>
  );
};

export default AgentRoutingDirectory;

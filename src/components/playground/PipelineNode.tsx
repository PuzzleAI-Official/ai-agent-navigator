import { motion } from "framer-motion";
import type { PipelineNodeState } from "@/types/activity";

interface Props {
  node: PipelineNodeState;
}

export function PipelineNode({ node }: Props) {
  const isActive = node.status === "active";
  const isCompleted = node.status === "completed";
  const isPending = node.status === "pending";

  return (
    <div className="relative flex flex-col items-center gap-2 px-3 py-1">
      {/* Node circle */}
      <motion.div
        className={`w-10 h-10 rounded-full border-[1.5px] flex items-center justify-center transition-all ${
          isActive
            ? "border-blue-400/40 bg-blue-400/8"
            : isCompleted
            ? "border-emerald-400/30 bg-emerald-400/5"
            : "border-white/[0.08] bg-white/[0.03]"
        }`}
        animate={
          isActive
            ? {
                boxShadow: [
                  "0 0 0px rgba(96,165,250,0)",
                  "0 0 16px rgba(96,165,250,0.15)",
                  "0 0 0px rgba(96,165,250,0)",
                ],
              }
            : {}
        }
        transition={isActive ? { duration: 2.5, repeat: Infinity } : {}}
      >
        {isCompleted ? (
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M3 7l3 3 5-5" stroke="rgb(110,231,183)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : isActive ? (
          <motion.div
            className="w-2 h-2 bg-blue-400/80 rounded-full"
            animate={{ scale: [1, 1.4, 1], opacity: [0.7, 1, 0.7] }}
            transition={{ duration: 2, repeat: Infinity }}
          />
        ) : (
          <div className="w-1.5 h-1.5 bg-white/10 rounded-full" />
        )}
      </motion.div>

      {/* Label */}
      <span
        className={`text-[11px] font-grotesk font-medium tracking-[0.04em] whitespace-nowrap ${
          isActive ? "text-white/90" : isCompleted ? "text-white/50" : "text-white/15"
        }`}
      >
        {node.label}
      </span>

      {/* Cost */}
      {isCompleted && node.cost != null && (
        <motion.span initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-[10px] font-mono text-white/30">
          {Math.round(node.cost * 20)} credits
        </motion.span>
      )}
    </div>
  );
}

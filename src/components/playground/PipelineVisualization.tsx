import { motion } from "framer-motion";
import { PipelineNode } from "./PipelineNode";
import type { PipelineNodeState } from "@/types/activity";

interface Props {
  nodes: PipelineNodeState[];
  visible: boolean;
}

export function PipelineVisualization({ nodes, visible }: Props) {
  if (!visible || nodes.length === 0) return null;

  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: "auto", opacity: 1 }}
      transition={{ duration: 0.5 }}
      className="border-b border-white/[0.06] bg-white/[0.02] backdrop-blur-sm px-6 py-5"
    >
      <div className="flex items-center justify-center gap-0">
        {nodes.map((node, i) => (
          <div key={node.agentId} className="flex items-center">
            {i > 0 && (
              <motion.div
                className={`w-10 lg:w-14 h-[1px] ${
                  node.status !== "pending" ? "bg-blue-400/30" : "bg-white/[0.06]"
                }`}
                initial={{ scaleX: 0 }}
                animate={{ scaleX: 1 }}
                transition={{ duration: 0.4, delay: i * 0.1 }}
                style={{ transformOrigin: "left" }}
              />
            )}
            <PipelineNode node={node} />
          </div>
        ))}
      </div>
    </motion.div>
  );
}

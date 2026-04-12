import { useState, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";

interface Props {
  message?: string;
}

const STATUS_MESSAGES = [
  "Scanning API directories and developer platforms",
  "Analyzing comparison articles and reviews",
  "Evaluating compatibility with your workflow",
  "Cross-referencing pricing and capabilities",
  "Identifying the most relevant candidates",
  "Generating evaluation test cases",
];

export function SearchingVisualization({ message }: Props) {
  const [msgIndex, setMsgIndex] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setMsgIndex((prev) => (prev + 1) % STATUS_MESSAGES.length);
    }, 3500);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="h-full flex flex-col items-center justify-center relative overflow-hidden">
      {/* Deep radial glow layers */}
      <div className="absolute w-[600px] h-[600px] rounded-full" style={{
        background: "radial-gradient(circle, rgba(59,130,246,0.04) 0%, transparent 60%)",
      }} />
      <div className="absolute w-[300px] h-[300px] rounded-full" style={{
        background: "radial-gradient(circle, rgba(139,92,246,0.03) 0%, transparent 60%)",
      }} />

      {/* Morphing geometric shape */}
      <div className="relative w-[280px] h-[280px] mb-12">
        {/* Outer morphing ring */}
        <motion.svg
          viewBox="0 0 200 200"
          className="absolute inset-0 w-full h-full"
        >
          <defs>
            <linearGradient id="grad1" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stopColor="rgba(96,165,250,0.25)" />
              <stop offset="50%" stopColor="rgba(139,92,246,0.15)" />
              <stop offset="100%" stopColor="rgba(96,165,250,0.25)" />
            </linearGradient>
            <linearGradient id="grad2" x1="100%" y1="0%" x2="0%" y2="100%">
              <stop offset="0%" stopColor="rgba(139,92,246,0.2)" />
              <stop offset="100%" stopColor="rgba(96,165,250,0.1)" />
            </linearGradient>
          </defs>

          {/* Morphing outer path */}
          <motion.path
            d="M100,20 C140,20 180,60 180,100 C180,140 140,180 100,180 C60,180 20,140 20,100 C20,60 60,20 100,20Z"
            fill="none"
            stroke="url(#grad1)"
            strokeWidth="0.5"
            animate={{
              d: [
                "M100,20 C145,25 175,55 180,100 C185,145 155,175 100,180 C55,185 25,155 20,100 C15,45 55,15 100,20Z",
                "M100,15 C150,20 185,50 180,100 C175,150 145,185 100,185 C50,180 15,150 20,100 C25,50 50,15 100,15Z",
                "M100,25 C140,15 180,55 175,100 C170,145 140,175 100,178 C60,180 25,145 22,100 C18,55 60,25 100,25Z",
                "M100,20 C145,25 175,55 180,100 C185,145 155,175 100,180 C55,185 25,155 20,100 C15,45 55,15 100,20Z",
              ],
            }}
            transition={{ duration: 12, repeat: Infinity, ease: "easeInOut" }}
          />

          {/* Inner morphing path */}
          <motion.path
            d="M100,45 C125,45 155,75 155,100 C155,125 125,155 100,155 C75,155 45,125 45,100 C45,75 75,45 100,45Z"
            fill="none"
            stroke="url(#grad2)"
            strokeWidth="0.4"
            animate={{
              d: [
                "M100,40 C130,45 160,70 155,100 C150,130 125,160 100,158 C70,155 40,130 42,100 C45,70 70,40 100,40Z",
                "M100,48 C125,40 158,72 155,100 C152,128 125,155 100,153 C75,155 45,125 48,100 C50,72 78,48 100,48Z",
                "M100,42 C128,48 152,75 155,100 C158,125 128,155 100,155 C72,152 42,128 45,100 C48,72 72,42 100,42Z",
                "M100,40 C130,45 160,70 155,100 C150,130 125,160 100,158 C70,155 40,130 42,100 C45,70 70,40 100,40Z",
              ],
            }}
            transition={{ duration: 10, repeat: Infinity, ease: "easeInOut" }}
          />
        </motion.svg>

        {/* Rotating dash ring */}
        <motion.svg
          viewBox="0 0 200 200"
          className="absolute inset-0 w-full h-full"
          animate={{ rotate: 360 }}
          transition={{ duration: 30, repeat: Infinity, ease: "linear" }}
        >
          <circle
            cx="100" cy="100" r="70"
            fill="none"
            stroke="rgba(255,255,255,0.04)"
            strokeWidth="0.5"
            strokeDasharray="3 8"
          />
        </motion.svg>

        {/* Counter-rotating dash ring */}
        <motion.svg
          viewBox="0 0 200 200"
          className="absolute inset-0 w-full h-full"
          animate={{ rotate: -360 }}
          transition={{ duration: 20, repeat: Infinity, ease: "linear" }}
        >
          <circle
            cx="100" cy="100" r="50"
            fill="none"
            stroke="rgba(255,255,255,0.03)"
            strokeWidth="0.5"
            strokeDasharray="2 12"
          />
        </motion.svg>

        {/* Orbiting accent dots */}
        {[
          { r: 70, dur: 8, size: 5, color: "rgba(96,165,250,0.5)" },
          { r: 70, dur: 8, size: 3, color: "rgba(139,92,246,0.3)", delay: 4 },
          { r: 50, dur: 6, size: 4, color: "rgba(96,165,250,0.3)", delay: 2 },
        ].map((dot, i) => (
          <motion.div
            key={i}
            className="absolute left-1/2 top-1/2"
            style={{ width: 0, height: 0 }}
            animate={{ rotate: 360 }}
            transition={{ duration: dot.dur, delay: dot.delay || 0, repeat: Infinity, ease: "linear" }}
          >
            <div
              className="rounded-full"
              style={{
                width: dot.size,
                height: dot.size,
                backgroundColor: dot.color,
                transform: `translateX(${dot.r}px) translateY(-${dot.size / 2}px)`,
                boxShadow: `0 0 ${dot.size * 4}px ${dot.color}`,
              }}
            />
          </motion.div>
        ))}

        {/* Center breathing core */}
        <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2">
          <motion.div
            className="w-2 h-2 rounded-full"
            style={{ backgroundColor: "rgba(139,92,246,0.6)" }}
            animate={{
              scale: [1, 1.5, 1],
              opacity: [0.5, 0.8, 0.5],
              boxShadow: [
                "0 0 10px rgba(139,92,246,0.2)",
                "0 0 30px rgba(139,92,246,0.4)",
                "0 0 10px rgba(139,92,246,0.2)",
              ],
            }}
            transition={{ duration: 3, repeat: Infinity, ease: "easeInOut" }}
          />
        </div>
      </div>

      {/* Status text */}
      <motion.div
        className="text-center relative z-10 max-w-md"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.3 }}
      >
        <h3 className="font-display text-[18px] text-white/45 mb-4 tracking-[-0.01em]">
          {message || "Discovering AI solutions"}
        </h3>

        {/* Rotating status messages */}
        <div className="h-5 relative overflow-hidden">
          <AnimatePresence mode="wait">
            <motion.p
              key={msgIndex}
              className="text-[12px] text-white/20 font-sans absolute inset-x-0"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
              transition={{ duration: 0.4 }}
            >
              {STATUS_MESSAGES[msgIndex]}
            </motion.p>
          </AnimatePresence>
        </div>
      </motion.div>
    </div>
  );
}

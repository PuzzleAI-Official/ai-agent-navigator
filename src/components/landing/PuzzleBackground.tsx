import { motion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";
import puzzlePiece1 from "@/assets/puzzle-piece-1.png";
import puzzlePiece2 from "@/assets/puzzle-piece-2.png";
import puzzlePiece3 from "@/assets/puzzle-piece-3.png";
import puzzlePiece4 from "@/assets/puzzle-piece-4.png";

interface PieceConfig {
  src: string;
  // Starting position (scattered, zoomed in)
  startX: number;
  startY: number;
  startScale: number;
  startRotate: number;
  // Final assembled position
  endX: number;
  endY: number;
  endScale: number;
  endRotate: number;
  zIndex: number;
}

const pieces: PieceConfig[] = [
  {
    src: puzzlePiece1,
    startX: 0, startY: 0, startScale: 3.5, startRotate: 0,
    endX: -12, endY: -10, endScale: 0.7, endRotate: -5,
    zIndex: 4,
  },
  {
    src: puzzlePiece2,
    startX: 120, startY: -200, startScale: 2.8, startRotate: 15,
    endX: 14, endY: -8, endScale: 0.65, endRotate: 3,
    zIndex: 3,
  },
  {
    src: puzzlePiece3,
    startX: -150, startY: 180, startScale: 2.5, startRotate: -20,
    endX: -10, endY: 12, endScale: 0.6, endRotate: -2,
    zIndex: 2,
  },
  {
    src: puzzlePiece4,
    startX: 180, startY: 150, startScale: 2.2, startRotate: 30,
    endX: 16, endY: 14, endScale: 0.65, endRotate: 8,
    zIndex: 1,
  },
];

const PuzzleBackground = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ["start start", "end start"],
  });

  return (
    <div ref={containerRef} className="absolute inset-0 overflow-hidden pointer-events-none">
      <div className="sticky top-0 h-screen flex items-center justify-center">
        {pieces.map((piece, i) => (
          <PuzzlePiece key={i} piece={piece} progress={scrollYProgress} />
        ))}
      </div>
    </div>
  );
};

const PuzzlePiece = ({
  piece,
  progress,
}: {
  piece: PieceConfig;
  progress: ReturnType<typeof useScroll>["scrollYProgress"];
}) => {
  const x = useTransform(progress, [0, 0.6, 1], [piece.startX, piece.startX * 0.3, piece.endX]);
  const y = useTransform(progress, [0, 0.6, 1], [piece.startY, piece.startY * 0.3, piece.endY]);
  const scale = useTransform(progress, [0, 0.5, 1], [piece.startScale, piece.startScale * 0.6, piece.endScale]);
  const rotate = useTransform(progress, [0, 0.7, 1], [piece.startRotate, piece.startRotate * 0.3, piece.endRotate]);
  const opacity = useTransform(progress, [0, 0.05, 0.8, 1], [0.6, 0.85, 0.9, 1]);

  return (
    <motion.img
      src={piece.src}
      alt=""
      width={512}
      height={512}
      className="absolute w-[45vw] md:w-[30vw] lg:w-[22vw] max-w-[320px] h-auto"
      style={{
        x,
        y,
        scale,
        rotate,
        opacity,
        zIndex: piece.zIndex,
        filter: "drop-shadow(0 20px 60px rgba(80, 55, 30, 0.15)) drop-shadow(0 8px 20px rgba(60, 45, 30, 0.1))",
      }}
    />
  );
};

export default PuzzleBackground;

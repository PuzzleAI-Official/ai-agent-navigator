import { motion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";
import puzzlePiece1 from "@/assets/puzzle-piece-1.png";
import puzzlePiece2 from "@/assets/puzzle-piece-2.png";
import puzzlePiece3 from "@/assets/puzzle-piece-3.png";
import puzzlePiece4 from "@/assets/puzzle-piece-4.png";

interface PieceConfig {
  src: string;
  startX: number;
  startY: number;
  startScale: number;
  startRotate: number;
  endX: number;
  endY: number;
  endScale: number;
  endRotate: number;
}

const pieces: PieceConfig[] = [
  {
    src: puzzlePiece1,
    startX: 0, startY: -20, startScale: 2.8, startRotate: 0,
    endX: -90, endY: -70, endScale: 0.55, endRotate: -5,
  },
  {
    src: puzzlePiece2,
    startX: 300, startY: -350, startScale: 0, startRotate: 15,
    endX: 90, endY: -65, endScale: 0.5, endRotate: 3,
  },
  {
    src: puzzlePiece3,
    startX: -350, startY: 300, startScale: 0, startRotate: -20,
    endX: -85, endY: 70, endScale: 0.48, endRotate: -2,
  },
  {
    src: puzzlePiece4,
    startX: 350, startY: 300, startScale: 0, startRotate: 30,
    endX: 95, endY: 75, endScale: 0.5, endRotate: 8,
  },
];

const PuzzleBackground = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ["start start", "end end"],
  });

  return (
    <div ref={containerRef} className="absolute inset-0 z-10 overflow-hidden pointer-events-none">
      <div className="sticky top-0 h-screen flex items-center justify-center">
        {pieces.map((piece, i) => (
          <PuzzlePiece key={i} piece={piece} index={i} progress={scrollYProgress} />
        ))}
      </div>
    </div>
  );
};

const PuzzlePiece = ({
  piece,
  index,
  progress,
}: {
  piece: PieceConfig;
  index: number;
  progress: ReturnType<typeof useScroll>["scrollYProgress"];
}) => {
  // First piece: visible from start, zoomed in, then shrinks
  // Other pieces: fade in as scroll progresses
  const isCenter = index === 0;
  
  const x = useTransform(progress, [0, 0.4, 0.8], [piece.startX, piece.startX * 0.4, piece.endX]);
  const y = useTransform(progress, [0, 0.4, 0.8], [piece.startY, piece.startY * 0.4, piece.endY]);
  const scale = useTransform(
    progress,
    isCenter ? [0, 0.3, 0.8] : [0.15, 0.4, 0.8],
    isCenter ? [piece.startScale, piece.startScale * 0.6, piece.endScale] : [piece.startScale, piece.endScale * 0.5, piece.endScale]
  );
  const rotate = useTransform(progress, [0, 0.5, 0.8], [piece.startRotate, piece.startRotate * 0.3, piece.endRotate]);
  const opacity = useTransform(
    progress,
    isCenter ? [0, 0.02, 0.7, 0.85] : [0.1, 0.25, 0.6, 0.85],
    isCenter ? [0.35, 0.5, 0.8, 1] : [0, 0.4, 0.85, 1]
  );

  return (
    <motion.img
      src={piece.src}
      alt=""
      width={512}
      height={512}
      className="absolute w-[50vw] md:w-[32vw] lg:w-[24vw] max-w-[350px] h-auto"
      style={{
        x,
        y,
        scale,
        rotate,
        opacity,
        filter: "drop-shadow(0 20px 60px rgba(80, 55, 30, 0.15)) drop-shadow(0 8px 20px rgba(60, 45, 30, 0.1))",
      }}
    />
  );
};

export default PuzzleBackground;

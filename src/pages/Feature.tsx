import Navbar from "@/components/landing/Navbar";
import HowItWorks from "@/components/landing/HowItWorks";
import WhySection from "@/components/landing/WhySection";
import Footer from "@/components/landing/Footer";

const Feature = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <div className="pt-20">
        <HowItWorks />
        <WhySection />
      </div>
      <Footer />
    </div>
  );
};

export default Feature;

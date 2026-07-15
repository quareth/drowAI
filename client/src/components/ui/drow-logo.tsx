import drowLogo from '../../assets/drow-logo.png';

interface DrowLogoProps {
  size?: number;
  className?: string;
}

export function DrowLogo({ size = 32, className = "" }: DrowLogoProps) {
  return (
    <img 
      src={drowLogo}
      alt="DrowAI Logo"
      width={size}
      height={size}
      className={className}
      style={{ objectFit: 'contain' }}
    />
  );
}
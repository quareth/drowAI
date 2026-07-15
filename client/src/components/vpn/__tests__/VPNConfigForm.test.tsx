import { render, screen, fireEvent } from '@testing-library/react';
import { VPNConfigForm } from '@/components/vpn/VPNConfigForm';

describe('VPNConfigForm', () => {
  it('should render provider selection', () => {
    const onChange = vi.fn();
    render(<VPNConfigForm onConfigChange={onChange} />);
    expect(screen.getByText('VPN Provider')).toBeInTheDocument();
  });

  it('should validate manual input change', () => {
    const onChange = vi.fn();
    render(<VPNConfigForm onConfigChange={onChange} />);
    const textarea = screen.getByPlaceholderText('client\nremote <host> 1194\ndev tun');
    fireEvent.change(textarea, { target: { value: 'client\nremote 1.2.3.4 1194\ndev tun' } });
    expect(onChange).toHaveBeenCalled();
  });
});


/**
 * Admin account and session policy step for the standalone setup wizard.
 */
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Shield, Eye, EyeOff, AlertCircle, CheckCircle, User } from 'lucide-react';
import { SetupActions, SetupCallout, SetupStepHeader } from "@/components/setup/setup-ui";

interface SecurityConfig {
  session_timeout: number;
  admin_username: string;
  admin_email: string;
  admin_password: string;
}

interface SecurityStepProps {
  config: SecurityConfig;
  onUpdate: (data: Partial<SecurityConfig>) => void;
  onNext: () => void;
  onPrevious: () => void;
}

export function SecurityStep({ config, onUpdate, onNext, onPrevious }: SecurityStepProps) {
  const [showPassword, setShowPassword] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const handleInputChange = (field: keyof SecurityConfig, value: string | number) => {
    onUpdate({ [field]: value });
    if (errors[field]) {
      setErrors(prev => ({ ...prev, [field]: '' }));
    }
  };

  const validateEmail = (email: string): boolean => {
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    return emailRegex.test(email);
  };

  const handleNext = () => {
    const newErrors: Record<string, string> = {};
    
    if (!config.admin_username.trim()) {
      newErrors.admin_username = 'Admin username is required';
    } else if (config.admin_username.length < 3) {
      newErrors.admin_username = 'Username must be at least 3 characters long';
    }

    if (!config.admin_email.trim()) {
      newErrors.admin_email = 'Admin email is required';
    } else if (!validateEmail(config.admin_email)) {
      newErrors.admin_email = 'Please enter a valid email address';
    }

    if (!config.admin_password.trim()) {
      newErrors.admin_password = 'Admin password is required';
    } else if (config.admin_password.length < 8) {
      newErrors.admin_password = 'Password must be at least 8 characters long';
    }

    if (config.session_timeout < 5) {
      newErrors.session_timeout = 'Session timeout must be at least 5 minutes';
    }

    if (Object.keys(newErrors).length > 0) {
      setErrors(newErrors);
      return;
    }

    onNext();
  };

  return (
    <div className="space-y-6">
      <SetupStepHeader
        icon={Shield}
        title="Security"
        description="Create the first administrator and set the default session policy."
      />

      <SetupCallout>
        <div className="flex items-start gap-3">
          <CheckCircle className="mt-0.5 h-4 w-4 text-slate-400" />
          <div>
            <h3 className="font-medium text-slate-100">Platform secrets are generated automatically.</h3>
            <p className="mt-1 text-slate-400">
              JWT signing and encryption keys are written to deployment config outside the wizard UI.
            </p>
          </div>
        </div>
      </SetupCallout>

      <div className="space-y-6">
        <div className="space-y-4">
          <h3 className="flex items-center text-sm font-medium uppercase tracking-[0.14em] text-slate-500">
            <Shield className="w-5 h-5 mr-2" />
            Session Policy
          </h3>

          <div>
            <Label htmlFor="session_timeout">Session Timeout (minutes)</Label>
            <Input
              id="session_timeout"
              type="number"
              value={config.session_timeout}
              onChange={(e) => handleInputChange('session_timeout', parseInt(e.target.value) || 0)}
              placeholder="1800"
              min="5"
              max="43200"
              className={errors.session_timeout ? 'border-red-500' : ''}
            />
            {errors.session_timeout && (
              <p className="text-sm text-red-600 mt-1">{errors.session_timeout}</p>
            )}
            <p className="text-xs text-slate-500 mt-1">
              How long users stay logged in (5 minutes to 30 days)
            </p>
          </div>
        </div>

        {/* Admin Account */}
        <div className="space-y-4">
          <h3 className="flex items-center text-sm font-medium uppercase tracking-[0.14em] text-slate-500">
            <User className="w-5 h-5 mr-2" />
            Admin Account
          </h3>

          <div>
            <Label htmlFor="admin_username">Admin Username</Label>
            <Input
              id="admin_username"
              value={config.admin_username}
              onChange={(e) => handleInputChange('admin_username', e.target.value)}
              placeholder="admin"
              className={errors.admin_username ? 'border-red-500' : ''}
            />
            {errors.admin_username && (
              <p className="text-sm text-red-600 mt-1">{errors.admin_username}</p>
            )}
            <p className="text-xs text-slate-500 mt-1">
              Username for the primary administrator account
            </p>
          </div>

          <div>
            <Label htmlFor="admin_email">Admin Email</Label>
            <Input
              id="admin_email"
              type="email"
              value={config.admin_email}
              onChange={(e) => handleInputChange('admin_email', e.target.value)}
              placeholder="admin@drowai.local"
              className={errors.admin_email ? 'border-red-500' : ''}
            />
            {errors.admin_email && (
              <p className="text-sm text-red-600 mt-1">{errors.admin_email}</p>
            )}
            <p className="text-xs text-slate-500 mt-1">
              Email address for the admin account
            </p>
          </div>

          <div>
            <Label htmlFor="admin_password">Admin Password</Label>
            <div className="relative">
              <Input
                id="admin_password"
                type={showPassword ? 'text' : 'password'}
                value={config.admin_password}
                onChange={(e) => handleInputChange('admin_password', e.target.value)}
                placeholder="Enter a secure password"
                className={`pr-12 ${errors.admin_password ? 'border-red-500' : ''}`}
              />
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute inset-y-0 right-0 h-full px-3"
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </Button>
            </div>
            {errors.admin_password && (
              <p className="text-sm text-red-600 mt-1">{errors.admin_password}</p>
            )}
            <p className="text-xs text-slate-500 mt-1">
              Strong password with at least 8 characters
            </p>
          </div>
        </div>
      </div>

      <SetupCallout>
        <div className="flex items-start gap-3">
          <AlertCircle className="mt-0.5 h-4 w-4 text-slate-400" />
          <div>
            <h4 className="font-medium text-slate-100">Security reminder</h4>
            <ul className="mt-2 list-disc space-y-1 pl-4 text-slate-400">
              <li>Use a strong, unique password for the admin account</li>
              <li>Generated platform secrets are stored outside the wizard</li>
              <li>Consider shorter session timeouts for sensitive deployments</li>
              <li>You can change these settings later through the admin panel</li>
            </ul>
          </div>
        </div>
      </SetupCallout>

      <SetupActions>
        <Button variant="outline" onClick={onPrevious} className="border-slate-700 bg-slate-950 text-slate-200 hover:bg-slate-800 hover:text-white">
          Previous
        </Button>
        
        <Button onClick={handleNext}>
          Next
        </Button>
      </SetupActions>
    </div>
  );
}

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Activity, ArrowRight, LockKeyhole } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api } from '../api';
import type { Session } from '../api/schemas';
import { formString } from '../lib/form';

export function AuthScreen({ bootstrap }: { bootstrap: boolean }) {
  const queryClient = useQueryClient();
  const [mfaVisible, setMfaVisible] = useState(false);
  const mutation = useMutation({
    mutationFn: async (form: FormData) => bootstrap
      ? api.bootstrap(formString(form, 'displayName'), formString(form, 'email'), formString(form, 'password'), formString(form, 'homeName'))
      : api.login(formString(form, 'email'), formString(form, 'password'), formString(form, 'totp') || undefined),
    onSuccess: (session) => {
      queryClient.removeQueries({ predicate: (entry) => entry.queryKey[0] !== 'session' });
      queryClient.setQueryData<Session>(['session'], session);
    },
    onError: (error) => {
      if (error instanceof Error && /mfa|one-time/i.test(error.message)) setMfaVisible(true);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    mutation.mutate(new FormData(event.currentTarget));
  }

  return <main className="auth-page">
    <section className="auth-brand" aria-label="PowerMeter V2 introduction">
      <div className="brand-mark"><Activity aria-hidden="true" /></div>
      <p className="eyebrow">Authenticated sensor evidence</p>
      <h1>PowerMeter <span>V2</span></h1>
      <p>Live electrical awareness and committed energy history, grounded exclusively in your PZEM sensors.</p>
      <ul className="auth-points"><li>Live heartbeat measurements</li><li>Gap-aware committed History</li><li>Rate-versioned cost estimates</li></ul>
    </section>
    <section className="auth-card" aria-labelledby="auth-heading">
      <LockKeyhole aria-hidden="true" />
      <p className="eyebrow">{bootstrap ? 'First run' : 'Secure access'}</p>
      <h2 id="auth-heading">{bootstrap ? 'Create the owner account' : 'Welcome back'}</h2>
      <p>{bootstrap ? 'This first account is the protected product owner. Use a unique password.' : 'Sign in to your home energy monitor.'}</p>
      <form onSubmit={submit}>
        {bootstrap && <><div className="field"><label htmlFor="display-name">Display name</label><input id="display-name" name="displayName" required autoComplete="name" /></div><div className="field"><label htmlFor="home-name">Home name</label><input id="home-name" name="homeName" defaultValue="Home" required /></div></>}
        <div className="field"><label htmlFor="email">Email</label><input id="email" name="email" type="email" required autoComplete="username" /></div>
        <div className="field"><label htmlFor="password">Password</label><input id="password" name="password" type="password" required minLength={bootstrap ? 14 : 1} autoComplete={bootstrap ? 'new-password' : 'current-password'} /></div>
        {mfaVisible && <div className="field"><label htmlFor="totp">Authenticator code</label><input id="totp" name="totp" inputMode="numeric" pattern="[0-9]{6}" autoComplete="one-time-code" required aria-describedby="totp-hint" /><small id="totp-hint">Enter the six-digit code from your authenticator.</small></div>}
        {mutation.isError && <p className="form-error" role="alert">{mutation.error instanceof Error ? mutation.error.message : 'Authentication failed.'}</p>}
        <button className="button button-primary button-full" type="submit" disabled={mutation.isPending}>{mutation.isPending ? 'Please wait…' : bootstrap ? 'Create owner' : 'Sign in'}<ArrowRight aria-hidden="true" /></button>
      </form>
      <p className="security-note">Credentials stay on this server. Device secrets are never sent to the browser.</p>
    </section>
  </main>;
}

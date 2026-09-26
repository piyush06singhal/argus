import { describe, expect, it } from 'vitest';

import {
  describeIdentityScope,
  describeOidcError,
  isSafeReturnPath,
  loginUrl,
  parseCallbackParams,
} from '../oidc';

describe('loginUrl', () => {
  it('points at the API path when given one', () => {
    expect(loginUrl('/api/v1/auth/oidc/login')).toBe('/api/v1/auth/oidc/login');
  });

  it('falls back to the documented path rather than emitting a broken link', () => {
    expect(loginUrl('')).toBe('/api/v1/auth/oidc/login');
    expect(loginUrl('http://evil.test/steal')).toBe('/api/v1/auth/oidc/login');
  });

  it('carries a safe return path through', () => {
    expect(loginUrl('/api/v1/auth/oidc/login', '/incidents?status=OPEN')).toBe(
      '/api/v1/auth/oidc/login?return_to=%2Fincidents%3Fstatus%3DOPEN'
    );
  });

  it('drops an unsafe return path instead of sanitizing it', () => {
    expect(loginUrl('/api/v1/auth/oidc/login', 'https://evil.test')).toBe(
      '/api/v1/auth/oidc/login'
    );
    expect(loginUrl('/api/v1/auth/oidc/login', '//evil.test')).toBe(
      '/api/v1/auth/oidc/login'
    );
  });
});

describe('isSafeReturnPath', () => {
  it('accepts relative paths', () => {
    expect(isSafeReturnPath('/')).toBe(true);
    expect(isSafeReturnPath('/remediation/actions')).toBe(true);
  });

  it('refuses anything that could leave the origin', () => {
    expect(isSafeReturnPath(null)).toBe(false);
    expect(isSafeReturnPath('')).toBe(false);
    expect(isSafeReturnPath('incidents')).toBe(false);
    expect(isSafeReturnPath('https://evil.test')).toBe(false);
    expect(isSafeReturnPath('//evil.test')).toBe(false);
    expect(isSafeReturnPath('/\\evil.test')).toBe(false);
    expect(isSafeReturnPath('/ok\r\nX-Injected: 1')).toBe(false);
  });
});

describe('parseCallbackParams', () => {
  it('reads a code and state', () => {
    expect(parseCallbackParams('?code=abc&state=xyz')).toEqual({
      code: 'abc',
      state: 'xyz',
    });
  });

  it('reads a reason the API or provider added', () => {
    expect(parseCallbackParams('error=state_expired')).toEqual({
      error: 'state_expired',
    });
  });

  it('does not invent values from an empty query', () => {
    expect(parseCallbackParams('')).toEqual({});
  });

  it('handles a query string without its leading question mark', () => {
    expect(parseCallbackParams('code=a&state=b')).toEqual({ code: 'a', state: 'b' });
  });
});

describe('describeOidcError', () => {
  it('explains every refusal class the backend can produce', () => {
    for (const reason of [
      'oidc_disabled',
      'state_unknown',
      'state_already_used',
      'state_expired',
      'token_exchange_failed',
      'id_token_invalid',
      'nonce_mismatch',
      'email_not_verified',
      'email_domain_not_allowed',
      'identity_disabled',
      'claims_invalid',
    ]) {
      const text = describeOidcError(reason);
      expect(text.length).toBeGreaterThan(20);
      //: An explanation must not leak the code as the whole message.
      expect(text).not.toBe(reason);
    }
  });

  it('shows an unrecognised code verbatim rather than inventing a cause', () => {
    expect(describeOidcError('brand_new_reason')).toContain('brand_new_reason');
  });

  it('still says something useful when no reason was given', () => {
    expect(describeOidcError(null)).toContain('Start again');
  });
});

describe('describeIdentityScope', () => {
  it('describes an admin as unrestricted', () => {
    expect(
      describeIdentityScope({ role: 'ADMIN', project_ids: [], unrestricted: true })
    ).toBe('every project (ADMIN)');
  });

  it('counts grants, and says so plainly when there are none', () => {
    expect(
      describeIdentityScope({ role: 'VIEWER', project_ids: ['a'] })
    ).toBe('1 granted project');
    expect(
      describeIdentityScope({ role: 'VIEWER', project_ids: ['a', 'b'] })
    ).toBe('2 granted projects');
    expect(describeIdentityScope({ role: 'VIEWER', project_ids: [] })).toBe(
      'no project grants'
    );
  });
});

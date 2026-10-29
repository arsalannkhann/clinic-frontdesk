/**
 * Typed API client. Paths, methods, path/query parameters, bodies and responses are checked
 * at compile time against `schema.ts`, generated from docs/openapi.json (`npm run gen:api`).
 *
 * The staff session token travels only in the Authorization header: never in the URL, a
 * cookie or a log. A 401 on a request that carried a token ends the session.
 */
import { ApiError, NETWORK_MESSAGE, UNREADABLE_MESSAGE, toApiError } from "./errors";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Method = "get" | "post" | "put" | "patch" | "delete";
export type ApiPath = keyof paths;

/** Methods the contract defines for a path. */
export type MethodsOf<P extends ApiPath> = {
  [M in Method]: undefined extends paths[P][M] ? never : M;
}[Method];

type Op<P extends ApiPath, M extends Method> = NonNullable<paths[P][M]>;
type PathParams<O> = O extends { parameters: { path: infer X } } ? X : never;
type QueryParams<O> = O extends { parameters: { query?: infer Q } } ? Q : never;
type Body<O> = O extends { requestBody?: infer R }
  ? NonNullable<R> extends { content: { "application/json": infer B } }
    ? B
    : never
  : never;
type Responses<O> = O extends { responses: infer R } ? R : never;
type Success<O> = Responses<O>[Extract<keyof Responses<O>, 200 | 201 | 202 | 204>];
type Json<R> = R extends { content: { "application/json": infer J } } ? J : undefined;

export type QueryOf<P extends ApiPath, M extends MethodsOf<P>> = NonNullable<
  QueryParams<Op<P, M>>
>;
export type ResponseOf<P extends ApiPath, M extends MethodsOf<P>> = Json<Success<Op<P, M>>>;

export interface RequestOptions<P extends ApiPath, M extends MethodsOf<P>> {
  params?: PathParams<Op<P, M>>;
  query?: QueryParams<Op<P, M>>;
  body?: Body<Op<P, M>>;
}

export interface ClientConfig {
  /** API origin (and optional prefix) without a trailing slash; "" for same origin. */
  baseUrl: string;
  getToken: () => string | null;
  /** Called after a 401 on a request that carried a token. */
  onUnauthorized: () => void;
  fetchImpl?: typeof fetch;
}

type Scalar = string | number | boolean | null | undefined;

export function buildUrl(
  baseUrl: string,
  path: string,
  params?: Readonly<Record<string, Scalar>>,
  query?: Readonly<Record<string, Scalar>>,
): string {
  const filled = path.replace(/\{([A-Za-z0-9_]+)\}/g, (_, name: string) => {
    const value = params?.[name];
    if (value === undefined || value === null || value === "") {
      throw new Error(`Missing path parameter: ${name}`);
    }
    return encodeURIComponent(String(value));
  });
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") search.append(key, String(value));
  }
  const qs = search.toString();
  return `${baseUrl.replace(/\/+$/, "")}${filled}${qs ? `?${qs}` : ""}`;
}

export class ApiClient {
  readonly #config: ClientConfig;

  constructor(config: ClientConfig) {
    this.#config = config;
  }

  async request<P extends ApiPath, M extends MethodsOf<P>>(
    method: M,
    path: P,
    options: RequestOptions<P, M> = {},
  ): Promise<ResponseOf<P, M>> {
    const url = buildUrl(
      this.#config.baseUrl,
      path,
      options.params as Record<string, Scalar> | undefined,
      options.query as Record<string, Scalar> | undefined,
    );
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = this.#config.getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const init: RequestInit = {
      method: method.toUpperCase(),
      headers,
      credentials: "omit", // Bearer tokens only; never send or accept cookies.
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
    };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const fetchImpl = this.#config.fetchImpl ?? globalThis.fetch.bind(globalThis);
    let response: Response;
    try {
      response = await fetchImpl(url, init);
    } catch {
      throw new ApiError(0, NETWORK_MESSAGE);
    }
    if (!response.ok) {
      const error = await toApiError(response, token !== null);
      if (response.status === 401 && token !== null) this.#config.onUnauthorized();
      throw error;
    }
    if (response.status === 204) return undefined as ResponseOf<P, M>;
    try {
      return (await response.json()) as ResponseOf<P, M>;
    } catch {
      throw new ApiError(response.status, UNREADABLE_MESSAGE);
    }
  }
}

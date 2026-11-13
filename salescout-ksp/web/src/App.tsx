import { Fragment, useCallback, useEffect, useState } from "react"
import { Loader2, Square } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

type Account = {
  id: number
  username: string
  role: "admin" | "user"
  disabled: boolean
}

type QueryRow = {
  id: number
  name: string
  owner_name?: string | null
  status: "queued" | "running" | "waiting" | "stopped" | "finished" | "error"
  checks_planned: number
  checks_done: number
  interval_seconds: number
  created_at: string
  next_run_at: string | null
  last_error: string | null
  progress: string | null
  last_thousand_ms: number | null
  cards: number
  search_total: number | null
}

type Stars = Record<string, number>

type Merchant = {
  merchant_id: string
  merchant_name: string
  price: number | null
  stock: number | null
  delivery: { type: string; label: string; cost: number | null }[]
}

type Observation = {
  observed_at: string
  stock_total: number | null
  review_count: number | null
  stars: Stars
  outcome?: {
    "1": number
    "2": number
    "3": number
    "4": number
    "5": number
    bad: number
    good: number
    neutral: number
    purchases: number
    returns: number
  }
  merchants: Merchant[]
  change_note: string
}

type CardView = {
  id: number
  product_id: string
  title: string
  url: string | null
  price: number | null
  rating: number | null
  image: string | null
  category: string | null
  cities: string | null
  listing_reviews: number | null
  latest: Observation | null
  history: Observation[]
}

type Supplier = {
  merchant_id: string
  merchant_name: string
  card_count: number
  stock: number | null
  card_reviews: number | null
  price_min: number | null
  price_max: number | null
  deliveries: string[]
  cards: { product_id: string; title: string }[]
}

type Detail = {
  query: QueryRow
  cards: CardView[]
  suppliers: Supplier[]
  delivery_groups: { label: string; rows: { title: string; product_id: string; merchant_name: string; stock: number | null; price: number | null }[] }[]
  overview: {
    cards: number
    search_total: number | null
    price_min: number | null
    price_max: number | null
    review_sum: number | null
    purchases: number | null
    returns: number | null
    stock_sum: number | null
    sellers: number
  }
}

const STATUS: Record<QueryRow["status"], string> = {
  queued: "в очереди",
  running: "идёт проверка",
  waiting: "ждёт следующий заход",
  stopped: "остановлен",
  finished: "проверки закончены",
  error: "ошибка",
}

function money(value: number | null | undefined) {
  if (value == null) return "—"
  return new Intl.NumberFormat("ru-KZ").format(value) + " ₸"
}

function count(value: number | null | undefined) {
  if (value == null) return "—"
  return new Intl.NumberFormat("ru-KZ").format(value)
}

function when(iso: string | null | undefined) {
  if (!iso) return "—"
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Asia/Qyzylorda",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso))
}

function starsLine(stars: Stars | undefined) {
  if (!stars) return "—"
  return [1, 2, 3, 4, 5].map((mark) => `${mark}★ ${stars[String(mark)] ?? 0}`).join("  ")
}

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function readJson<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set("Content-Type", "application/json")
  headers.set("X-Requested-With", "kaspi")
  const response = await fetch(url, { ...init, headers, credentials: "same-origin" })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : "Запрос не выполнен"
    throw new ApiError(response.status, detail)
  }
  return payload as T
}

export default function App() {
  const [queries, setQueries] = useState<QueryRow[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [name, setName] = useState("")
  const [checks, setChecks] = useState(3)
  const [interval, setIntervalMinutes] = useState(30)
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [openCard, setOpenCard] = useState<number | null>(null)
  const [me, setMe] = useState<Account | null | undefined>(undefined)

  const loadQueries = useCallback(async () => {
    try {
      const payload = await readJson<{ queries: QueryRow[] }>("/api/queries")
      setQueries(payload.queries)
      setListError(null)
      setSelectedId((current) => current ?? payload.queries[0]?.id ?? null)
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        const still = await readJson<{ user: Account }>("/api/me").catch(() => null)
        if (!still) setMe(null)
        return
      }
      setListError(error instanceof Error ? error.message : "Не удалось открыть список запросов")
    }
  }, [])

  const loadDetail = useCallback(async (id: number) => {
    try {
      const payload = await readJson<Detail>(`/api/queries/${id}`)
      setDetail(payload)
      setDetailError(null)
    } catch (error) {
      setDetailError(error instanceof Error ? error.message : "Не удалось открыть запрос")
    }
  }, [])

  useEffect(() => {
    void readJson<{ user: Account }>("/api/me")
      .then((payload) => setMe(payload.user))
      .catch(() => setMe(null))
  }, [])

  useEffect(() => {
    if (!me) return
    void loadQueries()
  }, [me, loadQueries])

  useEffect(() => {
    if (!me || selectedId == null) return
    void loadDetail(selectedId)
    const timer = window.setInterval(() => {
      void loadQueries()
      void loadDetail(selectedId)
    }, 3000)
    return () => window.clearInterval(timer)
  }, [selectedId, loadDetail, loadQueries])

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault()
    setFormError(null)
    setSubmitting(true)
    try {
      const payload = await readJson<{ query: QueryRow }>("/api/queries", {
        method: "POST",
        body: JSON.stringify({ name, checks, interval_minutes: interval }),
      })
      setName("")
      setSelectedId(payload.query.id)
      await loadQueries()
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Не удалось запустить поиск")
    } finally {
      setSubmitting(false)
    }
  }

  async function onStop(id: number) {
    await readJson(`/api/queries/${id}/stop`, { method: "POST" })
    await loadQueries()
    await loadDetail(id)
  }

  const active = queries?.find((item) => item.id === selectedId) ?? null

  async function onLogout() {
    await readJson("/api/logout", { method: "POST" })
    setMe(null)
    setQueries(null)
    setDetail(null)
    setSelectedId(null)
  }

  if (me === undefined) {
    return <p className="px-8 py-10 text-sm text-muted">Проверяю вход</p>
  }
  if (me === null) {
    return <Login onSuccess={setMe} />
  }

  return (
    <div className="min-h-screen bg-paper text-ink">
      <header className="border-b border-line px-4 py-5 sm:px-8">
        <p className="text-xs uppercase tracking-[0.18em] text-muted">Kaspi, все города</p>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h1 className="mt-1 font-serif text-3xl text-ink sm:text-4xl">Наблюдение за карточками</h1>
          <div className="flex items-center gap-3 text-sm">
            <span>{me.username}{me.role === "admin" ? ", администратор" : ""}</span>
            <Button type="button" variant="outline" size="sm" onClick={() => void onLogout()}>Выйти</Button>
          </div>
        </div>
        <p className="mt-2 max-w-2xl text-sm text-muted">
          Наименование ищет карточки сразу по всем городам Kaspi. Каждая проверка читает склад и число отзывов. Повтор идёт сам, пока не
          кончатся заходы или вы не остановите поиск. Новая строка в истории пишется только когда цифры изменились.
        </p>
      </header>

      <main className="grid gap-6 px-4 py-6 sm:px-8 lg:grid-cols-[320px_1fr]">
        <section className="space-y-4">
          <form onSubmit={onSubmit} className="space-y-3 rounded-lg border border-line bg-card p-4">
            <label className="block text-sm">
              Наименование товара
              <Input className="mt-1" value={name} onChange={(event) => setName(event.target.value)} placeholder="аэрогриль WW-820S" required />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm">
                Проверок
                <Input className="mt-1" type="number" min={1} max={6} value={checks} onChange={(event) => setChecks(Number(event.target.value))} />
              </label>
              <label className="block text-sm">
                Промежуток, мин
                <Input className="mt-1" type="number" min={10} max={1440} value={interval} onChange={(event) => setIntervalMinutes(Number(event.target.value))} />
              </label>
            </div>
            <Button type="submit" disabled={submitting} className="w-full">
              {submitting ? <Loader2 className="size-4 animate-spin" /> : null}
              Найти и проверять
            </Button>
            {formError ? <p className="text-sm text-accent">{formError}</p> : null}
            <p className="text-xs text-muted">Поиск сразу по всем городам. В обзор входят первые 12 разных карточек.</p>
          </form>
          {me.role === "admin" ? <UsersPanel /> : null}

          <div className="rounded-lg border border-line bg-card">
            <h2 className="border-b border-line px-4 py-3 text-sm font-medium">Запросы по наименованию</h2>
            {listError ? <p className="px-4 py-6 text-sm text-accent">{listError}</p> : null}
            {queries == null && !listError ? <p className="px-4 py-6 text-sm text-muted">Загружаю запросы</p> : null}
            {queries?.length === 0 ? <p className="px-4 py-6 text-sm text-muted">Пока нет ни одного поиска. Введите наименование слева.</p> : null}
            <ul>
              {queries?.map((query) => (
                <li key={query.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(query.id)}
                    className={`w-full border-b border-line px-4 py-3 text-left last:border-b-0 ${selectedId === query.id ? "bg-paper" : "hover:bg-paper/70"}`}
                  >
                    <span className="block font-medium">{query.name}{me.role === "admin" && query.owner_name ? ` · ${query.owner_name}` : ""}</span>
                    <span className="mt-1 block text-xs text-muted">
                      {STATUS[query.status]} · {query.checks_done} из {query.checks_planned} · карточек {query.cards}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className="min-w-0 space-y-4">
          {selectedId == null ? <p className="text-sm text-muted">Выберите запрос или запустите новый.</p> : null}
          {detailError ? <p className="text-sm text-accent">{detailError}</p> : null}
          {detail && active ? (
            <>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-serif text-2xl">{detail.query.name}</h2>
                  <p className="mt-1 text-sm text-muted">
                    {STATUS[detail.query.status]} · проверка {detail.query.checks_done} из {detail.query.checks_planned} · каждые{" "}
                    {Math.round(detail.query.interval_seconds / 60)} мин
                  </p>
                  <p className="mt-1 text-sm">{detail.query.progress || "ожидание"}</p>
                  {detail.query.last_error ? <p className="mt-1 text-sm text-accent">{detail.query.last_error}</p> : null}
                  {detail.query.next_run_at ? (
                    <p className="mt-1 text-sm text-muted">Следующий заход: {when(detail.query.next_run_at)} (Кызылорда)</p>
                  ) : null}
                </div>
                {["queued", "running", "waiting"].includes(detail.query.status) ? (
                  <Button type="button" variant="outline" onClick={() => void onStop(detail.query.id)}>
                    <Square className="size-3.5" />
                    Остановить
                  </Button>
                ) : (
                  <p className="text-sm text-muted">Повтор не идёт</p>
                )}
              </div>

              <div className="grid gap-3 sm:grid-cols-4">
                <Stat label="Карточек в обзоре" value={count(detail.overview.cards)} hint={detail.overview.search_total && detail.overview.search_total > detail.overview.cards ? `в выдаче ${detail.overview.search_total}` : "по этому наименованию"} />
                <Stat label="Склад, сумма" value={count(detail.overview.stock_sum)} hint="известные остатки продавцов" />
                <Stat label="Отзывы, сумма" value={count(detail.overview.review_sum)} hint="по карточкам, без повтора продавца" />
                <Stat label="Покупки по отзывам" value={count(detail.overview.purchases)} hint="отзывы на 2, 3, 4 и 5 звёзд" />
                <Stat label="Возвраты по отзывам" value={count(detail.overview.returns)} hint="отзывы на 1 звезду" />
                <Stat label="Цена" value={detail.overview.price_min == null ? "—" : `${money(detail.overview.price_min)} – ${money(detail.overview.price_max)}`} hint={`продавцов ${detail.overview.sellers}`} />
              </div>

              <div className="overflow-hidden rounded-lg border border-line bg-card">
                <h3 className="border-b border-line px-4 py-3 text-sm font-medium">Общий обзор карточек</h3>
                {detail.cards.length === 0 ? (
                  <p className="px-4 py-6 text-sm text-muted">Карточки ещё не записаны. Если поиск идёт, список появится по мере чтения.</p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[720px] text-left text-sm">
                      <thead className="text-xs uppercase tracking-wide text-muted">
                        <tr>
                          <th className="px-4 py-2 font-medium">Карточка</th>
                          <th className="px-4 py-2 font-medium">Цена</th>
                          <th className="px-4 py-2 font-medium">Отзывы</th>
                          <th className="px-4 py-2 font-medium">Склад</th>
                          <th className="px-4 py-2 font-medium">Изменение</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.cards.map((card) => {
                          const latest = card.latest
                          const reviews = latest?.review_count ?? card.listing_reviews
                          return (
                            <Fragment key={card.id}>
                              <tr className="border-t border-line align-top">
                                <td className="px-4 py-3">
                                  <button type="button" className="text-left font-medium hover:underline" onClick={() => setOpenCard(openCard === card.id ? null : card.id)}>
                                    {card.title}
                                  </button>
                                  <div className="mt-1 text-xs text-muted">{card.category || card.product_id}</div>
                                  {card.cities ? <div className="mt-1 text-xs text-muted">{card.cities}</div> : null}
                                </td>
                                <td className="px-4 py-3 whitespace-nowrap">{money(card.price)}</td>
                                <td className="px-4 py-3">
                                  <div>{count(reviews)}</div>
                                  <div className="mt-1 text-xs text-muted">{starsLine(latest?.stars)}</div>
                                  <div className="mt-1 text-xs">покупки {count(latest?.outcome?.purchases)} · возвраты {count(latest?.outcome?.returns)}</div>
                                </td>
                                <td className="px-4 py-3">{count(latest?.stock_total)}</td>
                                <td className="px-4 py-3">
                                  <div>{latest ? when(latest.observed_at) : "ещё не читали"}</div>
                                  <div className="mt-1 text-xs text-muted">{latest?.change_note}</div>
                                </td>
                              </tr>
                              {openCard === card.id ? (
                                <tr className="border-t border-line bg-paper/60">
                                  <td colSpan={5} className="px-4 py-3">
                                    <CardOpen card={card} />
                                  </td>
                                </tr>
                              ) : null}
                            </Fragment>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="overflow-hidden rounded-lg border border-line bg-card">
                <h3 className="border-b border-line px-4 py-3 text-sm font-medium">По поставщику</h3>
                {detail.suppliers.length === 0 ? (
                  <p className="px-4 py-6 text-sm text-muted">Поставщики появятся после чтения карточек.</p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[680px] text-left text-sm">
                      <thead className="text-xs uppercase tracking-wide text-muted">
                        <tr>
                          <th className="px-4 py-2 font-medium">Поставщик</th>
                          <th className="px-4 py-2 font-medium">Карточек</th>
                          <th className="px-4 py-2 font-medium">Склад</th>
                          <th className="px-4 py-2 font-medium">Отзывы карточек</th>
                          <th className="px-4 py-2 font-medium">Цена</th>
                          <th className="px-4 py-2 font-medium">Доставка</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.suppliers.map((supplier) => (
                          <tr key={supplier.merchant_id} className="border-t border-line align-top">
                            <td className="px-4 py-3 font-medium">{supplier.merchant_name}</td>
                            <td className="px-4 py-3">{supplier.card_count}</td>
                            <td className="px-4 py-3">{count(supplier.stock)}</td>
                            <td className="px-4 py-3">{count(supplier.card_reviews)}</td>
                            <td className="px-4 py-3 whitespace-nowrap">
                              {supplier.price_min == null ? "—" : supplier.price_min === supplier.price_max ? money(supplier.price_min) : `${money(supplier.price_min)} – ${money(supplier.price_max)}`}
                            </td>
                            <td className="px-4 py-3 text-xs text-muted">{supplier.deliveries.join(" · ") || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="space-y-3">
                <h3 className="text-sm font-medium">По сроку доставки</h3>
                {detail.delivery_groups.length === 0 ? <p className="text-sm text-muted">Сроки появятся вместе с продавцами.</p> : null}
                {detail.delivery_groups.map((group) => (
                  <div key={group.label} className="rounded-lg border border-line bg-card">
                    <h4 className="border-b border-line px-4 py-2 text-sm">{group.label}</h4>
                    <ul>
                      {group.rows.map((row, index) => (
                        <li key={`${row.product_id}-${row.merchant_name}-${index}`} className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line px-4 py-2 text-sm last:border-b-0">
                          <span>
                            {row.title}
                            <span className="text-muted"> · {row.merchant_name}</span>
                          </span>
                          <span className="text-muted">
                            {money(row.price)} · склад {count(row.stock)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </>
          ) : selectedId != null && !detailError ? (
            <p className="text-sm text-muted">Открываю запрос</p>
          ) : null}
        </section>
      </main>
      <footer className="border-t border-line px-4 py-4 text-sm sm:px-8">
        Время обработки запроса на 1000 карточек:{" "}
        <span className="font-medium">
          {detail?.query.last_thousand_ms != null ? `${count(detail.query.last_thousand_ms)} мс` : "—"}
        </span>
      </footer>
    </div>
  )
}

function Login({ onSuccess }: { onSuccess: (user: Account) => void }) {
  const [username, setUsername] = useState("root")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault()
    setPending(true)
    setError(null)
    try {
      const payload = await readJson<{ user: Account }>("/api/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      })
      onSuccess(payload.user)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось войти")
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-md items-center px-4">
      <form onSubmit={onSubmit} className="w-full space-y-3 rounded-lg border border-line bg-card p-6">
        <p className="text-xs uppercase tracking-[0.18em] text-muted">Kaspi, все города</p>
        <h1 className="font-serif text-3xl">Вход</h1>
        <p className="text-sm text-muted">Свои проверки видит только вошедший пользователь. Администратор ведёт список учётных записей.</p>
        <label className="block text-sm">
          Имя
          <Input className="mt-1" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required />
        </label>
        <label className="block text-sm">
          Пароль
          <Input className="mt-1" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required />
        </label>
        <Button type="submit" disabled={pending} className="w-full">{pending ? "Вхожу" : "Войти"}</Button>
        {error ? <p className="text-sm text-accent">{error}</p> : null}
      </form>
    </div>
  )
}

function UsersPanel() {
  const [users, setUsers] = useState<Account[] | null>(null)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [role, setRole] = useState<"user" | "admin">("user")
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    const payload = await readJson<{ users: Account[] }>("/api/users")
    setUsers(payload.users)
  }, [])

  useEffect(() => {
    void load().catch((caught: unknown) => setError(caught instanceof Error ? caught.message : "Не удалось открыть пользователей"))
  }, [load])

  async function onCreate(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    try {
      await readJson("/api/users", { method: "POST", body: JSON.stringify({ username, password, role }) })
      setUsername("")
      setPassword("")
      await load()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось создать пользователя")
    }
  }

  async function toggle(user: Account) {
    setError(null)
    try {
      await readJson(`/api/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ disabled: !user.disabled }) })
      await load()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось изменить пользователя")
    }
  }

  return (
    <div className="rounded-lg border border-line bg-card p-4">
      <h2 className="text-sm font-medium">Пользователи</h2>
      <form onSubmit={onCreate} className="mt-3 space-y-2">
        <Input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="имя" autoComplete="off" required />
        <Input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="пароль, от 8 знаков" autoComplete="new-password" required />
        <select className="h-10 w-full rounded-md border border-line bg-card px-3 text-sm" value={role} onChange={(event) => setRole(event.target.value as "user" | "admin")}>
          <option value="user">пользователь</option>
          <option value="admin">администратор</option>
        </select>
        <Button type="submit" variant="outline" className="w-full">Добавить</Button>
      </form>
      {error ? <p className="mt-2 text-sm text-accent">{error}</p> : null}
      <ul className="mt-3 space-y-2 text-sm">
        {users == null ? <li className="text-muted">Загружаю</li> : null}
        {users?.map((user) => (
          <li key={user.id} className="flex items-center justify-between gap-2">
            <span>{user.username} · {user.role === "admin" ? "администратор" : "пользователь"}{user.disabled ? " · отключён" : ""}</span>
            {user.username === "root" ? null : (
              <button type="button" className="text-xs text-accent" onClick={() => void toggle(user)}>
                {user.disabled ? "включить" : "отключить"}
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-lg border border-line bg-card px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 text-lg font-medium">{value}</div>
      <div className="mt-1 text-xs text-muted">{hint}</div>
    </div>
  )
}

function CardOpen({ card }: { card: CardView }) {
  const latest = card.latest
  return (
    <div className="space-y-3">
      {card.url ? (
        <a className="text-sm text-accent underline" href={card.url} target="_blank" rel="noreferrer">
          Карточка на Kaspi
        </a>
      ) : null}
      <div>
        <h4 className="text-xs uppercase tracking-wide text-muted">Оценки 1–5 и расчёт</h4>
        <p className="mt-1 text-sm">{starsLine(latest?.stars)}</p>
        <p className="mt-1 text-sm">
          Покупки, 2–5 звёзд: {count(latest?.outcome?.purchases)}. Возвраты, 1 звезда: {count(latest?.outcome?.returns)}. Один отзыв на 2, 3, 4 или 5 звёзд считается покупкой, отзыв на 1 звезду — возвратом.
        </p>
      </div>
      <div>
        <h4 className="text-xs uppercase tracking-wide text-muted">Продавцы этой карточки</h4>
        {latest?.merchants.length ? (
          <ul className="mt-2 space-y-1 text-sm">
            {latest.merchants.map((merchant) => (
              <li key={merchant.merchant_id}>
                {merchant.merchant_name} · {money(merchant.price)} · склад {count(merchant.stock)} · {merchant.delivery.map((item) => item.label).join("; ") || "срок не указан"}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-sm text-muted">Продавцы ещё не прочитаны.</p>
        )}
      </div>
      <div>
        <h4 className="text-xs uppercase tracking-wide text-muted">История изменений</h4>
        <ul className="mt-2 space-y-1 text-sm">
          {card.history.length === 0 ? <li className="text-muted">Изменений нет: карточка ещё не прочитана.</li> : null}
          {[...card.history].reverse().map((item) => (
            <li key={item.observed_at + item.change_note}>
              {when(item.observed_at)} (Кызылорда) — {item.change_note}. Склад {count(item.stock_total)}, отзывы {count(item.review_count)}.
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

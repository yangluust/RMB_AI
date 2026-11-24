---
title: >-
  Intervention under Incomplete Information:
  Fixing Responsiveness and Persistent Policy Errors
fontsize: 12pt
documentclass: article
papersize: a4
geometry: margin=1.2cm
linestretch: 1.02
header-includes:
  - \usepackage{amsmath,amssymb}
  - \usepackage{titlesec}
  - \titlespacing*{\section}{0pt}{10pt plus 2pt minus 2pt}{4pt}
  - \titlespacing*{\subsection}{0pt}{7pt plus 2pt minus 2pt}{3pt}
  - \setlength{\parskip}{4pt}
  - \setlength{\parindent}{0pt}
  - \setlength{\abovedisplayskip}{6pt}
  - \setlength{\belowdisplayskip}{6pt}
  - \makeatletter\renewcommand{\maketitle}{\begin{center}{\large\bfseries\@title\par}\end{center}\vspace{2pt}}\makeatother
---

This note develops a model of foreign exchange intervention when the
policymaker observes the CNY-USD rate and the CNH-CNY basis, but not the shocks
that move them. It has two main conclusions.

First, the CNH-CNY basis predicts state-dependent fixing responsiveness. For
the same appreciation in the onshore CNY-USD market during the previous
trading day, a CNH premium should lead the authority to resist more strongly
and pass less of the movement into today's fixing than a CNH discount. In
the intervention rule, the premium tilts the interpretation toward a financial
inflow and therefore toward a larger reserve purchase; the discount tilts it
toward a fundamental appreciation that should be accommodated more fully.
Empirically, this implies a negative interaction between the lagged market
movement and the basis. This mapping is reduced form because the model
instrument is reserve quantity and the model exchange rate is real, whereas
the observed fixing is a nominal price; Section 4 makes that bridge explicit.

Second, a temporary information error can create persistent allocation effects
through two external-asset stocks. Misreading the price pair can generate a
direct reserve-stock error $q_t^B$ because the fixed flow rule does not correct
inherited reserve gaps. A mistaken liquidity forecast can also move
the basis, the intermediation wedge, and the balance of payments temporarily,
leaving an NFA error $q_t^L$ after the wedge has closed. In the illustrative
over-saving case, both channels initially lower tradable consumption and
depreciate the real exchange rate while external assets accumulate. As the
temporary financing and liquidity distortions unwind, consumption recovers
and the exchange rate appreciates; the interest on the remaining asset stocks
then supports higher long-run consumption and a persistent real appreciation.
Thus the filtering error can decay without compounding even though the stock
and allocation effects persist.

# 1. Environment, information, and equilibrium

## 1.1 Units, primitives, and shocks

The model has three units of account and no onshore price level.

1. The nontradable good is the numeraire. Its price is one by definition, so
   no price symbol is needed. Onshore CNY is identified with this unit: one
   CNY buys one unit of the nontradable good at every date.
2. The dollar is identified with the tradable good. The law of one price holds
   with a constant dollar price of tradables, normalized to one.
3. CNH is the offshore currency, a distinct unit of account whose price in
   CNY is the endogenous basis $E_t$ (CNY per CNH).

Every quantity below is measured in one of these units.

| Symbol | Meaning | Unit |
|------------------------|--------------------------------------------|------------------------|
| $y_{NT}$, $c_{NT,t}$; $y_{T,t}$, $c_{T,t}$ | nontradable endowment and consumption; tradable endowment and consumption | nontradable; tradable (= dollar) |
| $S_t$ | onshore exchange rate, price of one dollar (one tradable) in CNY | CNY per dollar |
| $E_t$ | CNH-CNY basis, price of one CNH in CNY | CNY per CNH |
| $M_t$, $D_t$, $d^h_t$, $d^f_t$, $n_t$ | offshore reserves and deposits | CNH |
| $x_t=M_t/D_t$, $\phi$, $\psi$, $\lambda_t$ | reserve ratio, liquidity cost per deposit, policy parameter | unit-free |
| $B^{\$}_t$, $A_t$ | official reserves, net foreign assets | dollar |
| $R$, $R^m$, $R^d_t$, $R^{\$}$ | gross returns: onshore, CNH reserves, CNH deposits, dollar assets | CNY; CNH; CNH; dollar |
| $\mu_t$ | CNY-valued household liquidity coefficient | CNY |
| $\gamma$ | dollar-valued foreign-investor liquidity coefficient | dollar |
| $\Gamma_t=\gamma R^{\$}S_t/E_t$ | foreign liquidity weight scaled so that $\Gamma_t/d^f_t$ is a CNY gross-return gap | CNH |

There are three independent stationary shocks:

$$
\begin{aligned}
y_{T,t}&=\rho y_{T,t-1}+\varepsilon^y_t,\\
n_t&=\rho_n n_{t-1}+\varepsilon^n_t,\\
\mu_t&=\rho_\mu\mu_{t-1}+\varepsilon^\mu_t,
\end{aligned}
\qquad
\rho,\rho_n,\rho_\mu\in[0,1).
\tag{1}
$$

The endowment shock $y_T$ is fundamental. The noise-trader shock $n$ is a
dollar-funded financial flow. The household deposit shock $\mu$ changes CNH
deposit demand without a dollar flow.

## 1.2 Information, timing, and policy protocol

Let $\mathcal I_t^P$ be the policymaker's information set after date-$t$
prices have cleared and let $\mathcal I_t^i$ be the information set used by
private agent $i$ for equilibrium pricing. The maintained
incomplete-information assumption is
$\mathcal I_t^i=\mathcal I_t^P$. Accordingly,
$\mathbb E_t^P[\cdot]\equiv
\mathbb E[\cdot\mid\mathcal I_t^P]$ is also the private expectation used by
banks, households, and foreign investors. This common-information assumption
is imposed, not derived: agents are treated as atomistic and do not use their
own current positions to infer the aggregate shocks. Otherwise private
expectations of the next basis would differ from the policymaker's expectation,
and the basis would not identify the aggregate reserve ratio exactly.

Two counterfactual information sets are used below. The contemporaneous
full-information allocation in Section 2 uses $\mathbb E_t^{FI}$ and allows
date-$t$ policy to condition on the revealed date-$t$ shocks. The oracle
delayed policy in Section 5 uses $\mathbb E_t^O$: it observes the date-$t$
shock decomposition after markets clear but retains the baseline one-period
implementation delay. Thus `FI` denotes an allocation target, while `O`
isolates information errors holding implementation timing fixed.

Throughout, $\Delta B_t^{\$}\equiv B_t^{\$}-B_{t-1}^{\$}$ denotes the
change in the dollar reserve stock at date $t$. The reserve term in the
balance of payments (7) is
$S_t[\Delta B_t^{\$}-(R^{\$}-1)B_{t-1}^{\$}]$: the purchase less the
interest earned on the inherited stock. A policy that holds the stock
constant therefore spends the interest through the current account.

The baseline within-period sequence is:

1. **Beginning of $t$.** The information set $\mathcal I_{t-1}^P$ contains
   the public history of $(S,E)$ through $t-1$, past policy settings, and
   model parameters. It does not contain the date-$t$ innovations. Three settings are in place:
   $M_t=\bar xD_{t-1}$ corrects the deposit stock inferred from the last
   basis, $\lambda_t=\Lambda(\mathbb E_{t-1}^P[x_t])$ places the current
   parity point at the forecast reserve ratio, and $\Delta B_t^{\$}$ follows
   the lagged-price rule specified in Section 4. Hence
   $(M_t,B_t^{\$},\lambda_t)$ are predetermined.
2. **Shocks and market clearing.** The innovations in (1) arrive. Conditional
   on the predetermined settings, private quantities and the current prices
   $(S_t,E_t)$ are jointly determined by the equilibrium conditions below.
   Current prices do not change current policy.
3. **Observation and filtering.** After markets clear, the policymaker
   observes $(S_t,E_t)$. Given the known $\lambda_t$, the basis reveals
   $x_t$ and hence $D_t=M_t/x_t$. The two prices do not reveal the three
   innovations or the contemporaneous decomposition into household,
   foreign-investor, and noise-trader deposits.
4. **Policy updating.** The policymaker adds $(S_t,E_t)$ to
   $\mathcal I_t^P$, forms a posterior over the date-$t$ shocks, and sets
   $M_{t+1}=\bar xD_t$,
   $\lambda_{t+1}=\Lambda(\mathbb E_t^P[x_{t+1}])$, and the Section 4
   foreign-exchange rule for $B_{t+1}^{\$}$. Traders at $t$ price with
   $\mathbb E_t^P[E_{t+1}]=1$ because this updating rule is known, not because
   $\lambda_{t+1}$ is fixed before date-$t$ markets clear.

This assignment gives the instruments distinct roles. $M$ corrects the last
observed deposit-stock deviation, $\lambda$ accommodates predictable deposit
drift by relocating the expected parity point, and $B^{\$}$ manages the
external asset stock. In the contemporaneous full-information allocation,
$M_t=\bar xD_t$ instead keeps $x_t=\bar x$ state by state. The liquidity
parameter then remains at
$\bar\lambda\equiv\Lambda(\bar x)$, so
$\Delta\lambda_t^{FI}=0$: $\lambda$ is still needed to set the level of the
basis peg given $R^m=1$, but it is not an active stabilization instrument.
Under the oracle delayed policy, by contrast, predictable deposit drift
remains because $M_t$ is still based on $D_{t-1}$, so $\lambda_t^O$ can vary
even though the shock decomposition is known.

## 1.3 Household allocation and the real exchange rate

The representative domestic household has preferences over nontradable and
tradable consumption and the liquidity services of CNH deposits:

$$
\mathbb E_0\sum_{t\geq0}\beta^t
\left[
\log c_{NT,t}
+\iota\log c_{T,t}
+\frac{\mu_t}{y_{NT}}\log d^h_t
\right],
\qquad
\beta\in(0,1),\quad \iota>0,\quad \mu_t>0.
$$

The stochastic coefficient $\mu_t$ is measured in CNY, so
$\mu_t/y_{NT}$ is a unit-free liquidity weight. Preferences are additively
separable across the three arguments.

Because CNY is identified with the nontradable good, $S_t$ is the relative
price of tradables in nontradables. Nontradable-market clearing gives
$c_{NT,t}=y_{NT}$, and the household's intratemporal consumption condition
gives

$$
S_t=\frac{\iota y_{NT}}{c_{T,t}}. \tag{2}
$$

An increase in $S_t$ is a real depreciation. Equation (2) is central: an
intervention-induced movement in $S_t$ is an inverse movement in tradable
consumption. The model therefore evaluates intervention by its effect on $c_T$,
not by exchange rate stability itself. The nominal market rate and official
fixing, both in CNY per dollar with an onshore price level that can move, are
introduced only in Section 4, where they are mapped to $S_t$.

## 1.4 Offshore banks, liquidity policy, and the CNH-CNY basis

Competitive offshore banks issue CNH deposits
$D_t=d^h_t+d^f_t+n_t$. Onshore households hold $d^h_t$, foreign investors
hold $d^f_t$, and foreign noise traders hold $n_t$. Banks hold CNH liquidity
reserves $M_t$, and

$$
x_t\equiv\frac{M_t}{D_t}
$$

is the reserve-deposit ratio. Supplying deposits costs
$\phi(x_t,\lambda_t)D_t$, where $\lambda_t$ is a liquidity policy parameter.
Assume $\phi_x<0$, $\phi_{xx}>0$, $\phi_\lambda\geq0$, and
$\phi_{x\lambda}<0$: a higher $\lambda$ is a tighter liquidity stance that
does not lower the cost of supplying deposits at a given $x$ and raises the
cost of diluting reserves. Define the marginal deposit cost

$$
\psi(x,\lambda)\equiv\phi(x,\lambda)-x\phi_x(x,\lambda),
$$

so that $\psi_x=-x\phi_{xx}<0$ and $\psi_\lambda=\phi_\lambda-x\phi_{x\lambda}>0$.

The basis $E_t$ is the CNY price of one CNH, so $E_t>1$ is a CNH premium.
Offshore reserves pay the gross rate $R^m=1$ in CNH. Onshore assets pay
$R=1/\beta$ in CNY, where $\beta$ is the household discount factor; since CNY
is the nontradable good, $R$ is a real rate. A bank that takes one CNH deposit
at $t$ converts it to $E_t$ CNY, invests onshore, and after the marginal
liquidity cost $\psi_t$ can promise the CNH deposit return

$$
R^d_t=E_tR(1-\psi_t),
$$

given the expected-basis peg $\mathbb E^P_t[E_{t+1}]=1$ specified by the
protocol in Section 1.2.

Under that expected-basis peg,
$\mathbb E_t^P[E_{t+1}]=1$, banks' reserve demand is

$$
E_t=\frac{\beta}{1+\phi_x(x_t,\lambda_t)}. \tag{3}
$$

Equivalently, $E_t[1+\phi_x(x_t,\lambda_t)]=\beta$. The left-hand side is
the bank's net current CNY cost of one more CNH reserve: because
$\partial[\phi(M_t/D_t,\lambda_t)D_t]/\partial M_t=\phi_x<0$, the reserve
saves $-\phi_x$ CNH and costs $E_t(1+\phi_x)$ CNY net. The right-hand side is
its discounted expected CNY payoff,
$\beta R^m\mathbb E_t^P[E_{t+1}]=\beta$; no arbitrage equates the two sides.

**Liquidity policy and the expected basis peg.** Liquidity policy uses
$\lambda_t$ to peg the basis to one in expectation:

$$
\mathbb E_{t-1}^P[E_t]=1.
$$

This is not a state-by-state peg of the realized basis. By the timing in
Section 1.2, $\lambda_t$ is predetermined when offshore banks and depositors
trade at $t$; allowing it to adjust after observing $E_t$ could force the
realized basis back to one and erase its information about offshore liquidity.

The parity schedule $\Lambda(x)$ is defined by

$$
-\phi_x\bigl(x,\Lambda(x)\bigr)=1-\beta,
\qquad
\Lambda'(x)=-\frac{\phi_{xx}}{\phi_{x\lambda}}\geq0.
$$

At the forecast value of $x_t$, this schedule sets the marginal liquidity
benefit equal to the return disadvantage of CNH reserves. It therefore makes
the expected basis equal to one to first order. The policy rule and the
realized basis are

$$
\begin{aligned}
\lambda_t
  &=\Lambda\bigl(\mathbb E_{t-1}^P[x_t]\bigr),\\
\widehat E_t
  &\equiv E_t-1
  \simeq
  -\frac{\phi_{xx}}{\beta}
  \left(x_t-\mathbb E_{t-1}^P[x_t]\right),
\end{aligned} \tag{4}
$$

The reason $\lambda$ remains active under the delayed protocol is that
$M_t=\bar xD_{t-1}$ is based on the last observed deposit stock. Deposits can
drift predictably between $t-1$ and $t$: a noise position decays or an
endowment-induced change in $d^f$ unwinds. Hence
$\mathbb E_{t-1}^P[x_t]=\bar x\,D_{t-1}/\mathbb E_{t-1}^P[D_t]$ differs from
$\bar x$, and $\lambda_t$ absorbs this forecastable drift by placing the
parity point at $\mathbb E_{t-1}^P[x_t]$. Thus $M$ removes the realized level
deviation, $\lambda$ handles the predictable drift, and the realized basis in
(4) reveals only the surprise.

Because $\lambda_t$ is
predetermined and $\phi_{xx}>0$, equation (3) is invertible in $x_t$. A
deposit surprise lowers $x_t$ and produces a CNH premium; a reserve surprise
raises $x_t$ and produces a discount. The expected peg therefore anchors the
basis at one while allowing its realized deviation to reveal aggregate
offshore liquidity, though not the shock that caused it.

## 1.5 Deposit demand and market clearing

**Household demand.** The liquidity term specified in Section 1.3 has marginal
utility $\mu_t/(y_{NT}d^h_t)$. The CNY opportunity cost of holding one CNH
deposit is
$E_t-R^d_t\mathbb E^P_t[E_{t+1}]/R=E_t\psi_t$. Equating that cost, valued at
the marginal utility $1/y_{NT}$ of one CNY, to the marginal liquidity benefit
$\mu_t/(y_{NT}d^h_t)$ gives
$d^h_t=\mu_t/(E_t\psi_t)$ in CNH. Here $E_t$ converts the CNH liquidity wedge
$\psi_t$ into CNY. This demand is funded in CNY.

**Foreign demand and noise traders.** Foreign investors value the liquidity
services of CNH deposits. Following Reis, assume deep pockets, linear utility
over dollar consumption, and logarithmic utility from CNH liquidity:

$$
\mathbb E_0^P\sum_{t\geq0}(R^{\$})^{-t}
\left[c^*_{t}+\gamma\log d^f_t\right],
\qquad \gamma>0.
$$

Because $c_t^*$ enters linearly in dollars, $\gamma$ is a dollar-valued
liquidity coefficient rather than a unit-free weight. This is a cardinal
normalization of utility and has no independent economic content.

One CNH deposit costs $E_t/S_t$ dollars at $t$ and pays $R_t^dE_{t+1}/S_{t+1}$
dollars at $t+1$. Its first-order condition equates the marginal liquidity
service $\gamma/d^f_t$ to the expected dollar-return shortfall. Multiplying
this condition by $R^{\$}S_t/E_t$ and using the expected basis peg gives, to
first order,

$$
R^{\$}\mathbb E_t^P
\left[\frac{S_{t+1}}{S_t}\right]
-R(1-\psi_t)
=\frac{\Gamma_t}{d^f_t},
\qquad
\Gamma_t\equiv\gamma R^{\$}\frac{S_t}{E_t}.
$$

The first term is the expected CNY return on a dollar bond. The second is the
expected CNY return on a CNH deposit. Liquidity services allow the deposit to
pay the return gap on the right. Using $S_t=\iota y_{NT}/c_{T,t}$ and
$R=1/\beta$, and dividing by $R$, gives

$$
\beta R^{\$}\mathbb E_t^P
\left[\frac{c_{T,t}}{c_{T,t+1}}\right]-1
=\frac{\Gamma_t}{R d^f_t}-\psi_t. \tag{5}
$$

Equation (5) is liquidity-adjusted UIP expressed through tradable consumption.
The foreign liquidity premium $\Gamma_t/(R d^f_t)$ permits the CNH deposit to
pay less than a dollar bond, while the bank intermediation cost $\psi_t$
further lowers its return. All terms are unit-free. Unlike the primitive
preference weight $\gamma$, $\Gamma_t$ moves with the dollar price of a
CNH.

Noise-trader deposits $n_t$ are an exogenous, dollar-funded demand for CNH
deposits.

**Market clearing.** Banks supply $D_t=M_t/x_t$ deposits. Equating this supply
to household, foreign-investor, and noise-trader demand gives

$$
d^f_t
=\frac{M_t}{x_t}
-\frac{\mu_t}{E_t\psi_t}
-n_t,
\qquad
\psi_t\equiv\psi(x_t,\lambda_t). \tag{6}
$$

All terms in (6) are CNH balances. It gives the foreign-investor position
needed to absorb the deposits left after household and noise-trader demand;
in equilibrium, that residual position must also satisfy the pricing
condition (5). Although $\mu_t$ and $n_t$ both raise deposit demand, only
$n_t$ brings a dollar flow that foreign exchange intervention may need to
absorb.

## 1.6 Balance of payments and net foreign assets

The government holds dollar reserves $B^{\$}_t$ earning the exogenous gross
rate $R^{\$}$. Its balance of payments, written in CNY, is

$$
\begin{aligned}
S_t y_{T,t}-\iota y_{NT}
={}&S_t
  \left(B^{\$}_t-R^{\$}B^{\$}_{t-1}\right)\\
&-\left[
  E_t(d^f_t+n_t)
  -E_tR^d_{t-1}(d^f_{t-1}+n_{t-1})
  \right].
\end{aligned} \tag{7}
$$

Here $y_{T,t}$ is the tradable endowment, so the left side is the trade
balance: the CNY value of tradable output less tradable spending
$S_tc_{T,t}=\iota y_{NT}$ from (2). On the right, $S_t$ converts the dollar
reserve flow into CNY, and $E_t$ converts the CNH deposits issued at $t$ and
the CNH repayment $R^d_{t-1}(d^f_{t-1}+n_{t-1})$ of deposits issued at $t-1$
into CNY. The bracket is the net CNY inflow from foreign-held CNH deposits.
With $\Delta B^{\$}_t=0$ and no deposit flows, the reserve term equals
$-S_t(R^{\$}-1)B^{\$}_{t-1}$ and the trade balance is in deficit by the
interest on reserves: a constant reserve stock means that its interest is
consumed. Net foreign assets in dollars are

$$
A_t\equiv
B^{\$}_t-\frac{E_t(d^f_t+n_t)}{S_t}.
$$

Thus $B^{\$}_t$ absorbs dollar flows through the balance of payments.

## 1.7 Equilibrium and observability

Given inherited foreign positions and the predetermined policy settings
$(M_t,B_t^{\$},\lambda_t)$, equations (2)--(7) jointly determine
$(c_{T,t},S_t,E_t,x_t,d_t^h,d_t^f,D_t,R_t^d)$ for each realization of the
shocks in (1). After clearing, the policymaker observes $(S_t,E_t)$ and knows
its own settings. Inverting (3) recovers $x_t$, and therefore
$D_t=M_t/x_t$, exactly.

What remains hidden is the decomposition of that aggregate deposit stock and
the three innovations behind it. The basis supplies one signal through
offshore liquidity, while the onshore rate supplies a second through the
balance of payments. These two prices cannot identify three simultaneous
innovations. Section 3 characterizes this rank deficiency; Section 5 traces
the consequences of the resulting misclassification.

# 2. Contemporaneous full-information allocation

## 2.1 Benchmark responses to the three shocks

The benchmark allows date-$t$ policy to condition on the revealed date-$t$
shocks before intervention is settled. It is an allocation target, not the
implementable timing of Section 1.2. The liquidity leg chooses
$M_t=\bar xD_t$ and keeps $x_t=\bar x$ state by state. Consequently
$\lambda_t=\bar\lambda=\Lambda(\bar x)$ and
$\Delta\lambda_t^{FI}=0$: $\lambda$ implements the level of the expected
basis peg but has no state-contingent stabilization role. The foreign
exchange leg absorbs the dollar flow that should not alter the
permanent-income path of tradable consumption.

For a positive noise-trader inflow and a positive household deposit shock,
respectively, the benchmark responses are

$$
\begin{aligned}
\text{noise inflow:}\quad
&\Delta M_t=x n_t,
&&\Delta B^{\$}_t=\frac{E_tn_t}{S_t},\\
\text{household deposit demand:}\quad
&\Delta M_t=x\,\Delta d^h_t,
&&\Delta B^{\$}_t=0.
\end{aligned} \tag{8}
$$

The reserve purchase converts the CNH inflow into dollars at $E_t/S_t$;
with $x_t$ stabilized, $E_t=1$. Both shocks expand deposits, so both require
the liquidity leg. Only $n_t$ brings dollars, so only $n_t$ requires a
foreign exchange purchase. This
distinction is precisely what prices fail to reveal completely.

For an endowment shock, policy should let consumption respond to permanent
income and save only the transitory part of the windfall. It should not
stabilize the exchange rate.

**Foreign deposit response.** Hold $x$, and hence $\psi$ and the deposit
return $R^d$, fixed; then $E_t=1$ by (3). Full-information consumption is a
martingale to first order, so
$\mathbb E_t^{FI}[c_{T,t}/c_{T,t+1}]=1$. Equation (5), together with
$R=1/\beta$ and $R^d=R(1-\psi)$, then implies

$$
\frac{\Gamma_t}{d^f_t}
=R\left(\beta R^{\$}-1+\psi\right)
=R^{\$}-R^d.
$$

Since $\Gamma_t=\gamma R^{\$}S_t$ when $E_t=1$,

$$
d^f_t=\kappa S_t,
\qquad
\kappa\equiv\frac{\gamma R^{\$}}{R^{\$}-R^d}.
$$

Foreign investors therefore hold a constant dollar value
$d^f_t/S_t=\kappa$ of CNH deposits. A CNY appreciation lowers their CNH
balances one-for-one.

**Dollar budget and revaluation.** Subtracting the constant dollar liability
$\kappa$ from official reserves gives dollar net foreign assets
$A_t\equiv B^{\$}_t-d^f_t/S_t=B^{\$}_t-\kappa$. Dividing (7) by $S_t$ and
using $S_tc_{T,t}=\iota y_{NT}$ yields

$$
A_t-R^{\$}A_{t-1}
=y_{T,t}-c_{T,t}
+\gamma R^{\$}
-\kappa R^d\left(\frac{S_{t-1}}{S_t}-1\right). \tag{9}
$$

The first additional term is the constant liquidity rent:
$\gamma R^{\$}=\kappa(R^{\$}-R^d)$. The country borrows from foreign CNH
holders below the dollar return. 

The last term, $-\kappa R^d\left(\frac{S_{t-1}}{S_t}-1\right)$, is a revaluation loss.
Deposits issued at $t-1$ are repaid in CNH, so an unexpected appreciation
($S_t<S_{t-1}$) raises their dollar repayment cost. The term has zero
conditional expectation to first order because consumption, and hence $S$, is
a martingale on the full-information path. Once a date-$t$ consumption
innovation is realized, the revaluation loss is
$-\theta_0\Delta c_{T,t}$, where

$$
\theta_0\equiv\frac{R^d d^f}{\iota y_{NT}}.
$$

Thus $\theta_0$ measures the dollar value of foreign CNH deposits, with
interest, relative to tradable consumption. It is about one half in Reis's
calibration. Its annuity value is
$\theta\equiv[(R^{\$}-1)/R^{\$}]\theta_0$, which is of order one percent.

**Permanent income and policy responses.** Forward iteration of (9), under
the no-Ponzi condition, gives the full-information optimal consumption path
under the baseline managed-float anchor, conditional on fixed $x$ and the
inherited CNH liability stock:

$$
\begin{aligned}
c_{T,t}
  &=y^{\mathrm{perm}}_{T,t}
    +(R^{\$}-1)A_{t-1}
    +\gamma R^{\$}
    -\theta\,\Delta c_{T,t},\\
y^{\mathrm{perm}}_{T,t}
  &\equiv
  \frac{R^{\$}-1}{R^{\$}}
  \sum_{j\geq0}
  \frac{\mathbb E_t^{FI} y_{T,t+j}}{(R^{\$})^j}.
\end{aligned} \tag{10}
$$

For the AR(1) endowment shock in (1), the impact responses are

$$
\begin{aligned}
\Delta c_{T,t}
  &=\frac{1}{1+\theta}
    \frac{R^{\$}-1}{R^{\$}-\rho}\varepsilon^y_t,\\
\Delta B^{\$}_t
  &\simeq s(\rho)\varepsilon^y_t-\theta_0\,\Delta c_{T,t},
\qquad
s(\rho)\equiv\frac{1-\rho}{R^{\$}-\rho},\\
\Delta M_t
  &=x\,\Delta d^f_t
  =-x\,d^f\,\frac{\Delta c_{T,t}}{\bar c_T}.
\end{aligned} \tag{11}
$$

$\Delta B^{\$}_t$ is the residual reserve flow required to implement optimal consumption while keeping $x$ fixed, with the term $\theta \Delta c_{T,t}$ dropped in the approximation. The first term, $s(\rho)\varepsilon^y_t$, is the transitory part of the windfall
saved abroad. The second term, $\theta_0\,\Delta c_{T,t}$, finances the revaluation outflow to foreign deposit
holders. The change in $M_t$ keeps $x$ fixed as their CNH balances adjust.

For a transitory shock, $s(0)=1/R^{\$}$, so almost all of the windfall is
saved. The saving term dominates the small revaluation outflow, so the
foreign-exchange leg purchases dollars, $\Delta B^{\$}_t>0$. The small
appreciation nevertheless reduces foreign CNH balances, so the liquidity leg
withdraws CNH reserves to keep $x$ fixed, $\Delta M_t<0$.

As $\rho$ approaches one, $s(\rho)$ approaches zero: consumption absorbs the
windfall and CNY appreciates permanently. There is no permanent-income motive
to accumulate reserves, but the appreciation causes foreign investors to cash
out $\theta_0\Delta c_{T,t}$ dollars and reduce their CNH balances. Full
information therefore calls for $\Delta B^{\$}_t<0$ and $\Delta M_t<0$ after
a permanent shock.

The foreign exchange target $\Delta B^{\$}_t$ is therefore shock-specific. Full information
calls for absorbing all of $n_t$, none of $\mu_t$, and, for an endowment
shock, the saved fraction $s(\rho)$ net of the revaluation outflow
$\theta_0\Delta c_{T,t}$. The needed response depends both on the source of
the price movement and, for fundamentals, on its persistence.

## 2.2 The nominal anchor behind the fundamental-shock response

The responses in (11) that are specific to foreign CNH deposits -- the fall in
$d^f$, the reserve outflow $\theta_0\Delta c_{T,t}$, and the valuation factor
$\theta$ -- depend on the nominal anchor. This subsection states what that
anchor is, why it matters, and why the document adopts it.

**The anchor in the baseline.** Identifying onshore CNY with the nontradable
good fixes $P_{NT}\equiv1$. This is a nominal anchor: nontradable-price
stability under a float. Every real appreciation is therefore also a nominal
appreciation. Under flexible prices it is one admissible anchor among several.
A hard peg of the nominal exchange rate, with the real adjustment delivered by a rise in $P_{NT}$ alone, is another. 

**Why the anchor matters.** Without nominal claims, flexible-price anchor
choice is neutral: it determines whether a real appreciation appears through
the nominal exchange rate or the nontradable price, but not who consumes the
windfall. Foreign-held CNH deposits break this neutrality because their real
payoff depends on the dollar price of one CNH, $E_t/S_t$ in the baseline and
$E_t/E^{\$}_t$ in general. Under the float, a nominal appreciation therefore
has three effects: (i) foreign investors keep a constant dollar value of CNH,
so $d^f$ falls and $\Delta M_t<0$; (ii) the outstanding stock is revalued,
producing the dollar outflow $\theta_0\Delta c_{T,t}$ that reserves finance;
and (iii) the annuity value of that loss, $\theta$, lowers home consumption.
Under a hard peg with $P_{NT}$ adjusting, the dollar price of a CNH is
unchanged. Hence $d^f$, $M_t$, and $B^{\$}_t$ do not move, $\theta=0$, and the
home country consumes the full permanent increase.

Both anchors allow the fundamental shock to produce a real appreciation
rather than holding $S$ fixed. But they implement different real allocations:
Under the float, part of the windfall is transferred to foreign CNH holders,
reducing home consumption and increasing foreign consumption. Under the peg,
no such transfer occurs.

**Welfare.** Ex post, after a positive permanent shock, the home country is better
off under the peg because nothing leaks to foreign holders. Ex ante, the float
makes CNH liabilities behave like local-currency debt: home pays foreigners
more when it is rich and less when it is poor, which shifts part of the $y_T$
risk onto risk-neutral, deep-pocketed foreigners. A risk-averse home country
values that insurance. The liquidity operation itself is costless, the
planner's choice of $x_t$ does not depend on the anchor, and with log utility
the aggregate rent $\gamma R^{\$}$ is the same in either regime. Neither
anchor is first best; the ranking is a second-order risk-sharing effect.

**Practical choice.** The empirical object of this document is the daily
fixing and its pass-through from the market close. Under a hard peg there is
no fixing variation to study. The document therefore models the managed
float, in which persistent fundamentals move the nominal rate, and (i) to (iii)
are predictions for that regime. Section 4's caveat applies: to the extent
that part of the multi-year real adjustment runs through the onshore-US
inflation differential rather than the nominal rate, these responses are
correspondingly smaller.

# 3. What the two prices reveal

Let $\widehat S_t$ denote the proportional deviation of the real exchange
rate from its steady state; recall that $\widehat E_t=E_t-1$ is the basis
deviation.

Foreign deposit demand is highly elastic to expected exchange rate changes.
Linearizing (5) around the steady state, with $\widehat d^f_t$ a proportional
deviation, gives

$$
\widehat d^f_t
\simeq
\widehat S_t-\widehat E_t
+\eta\left(\widehat S_t-\mathbb E_t^P\widehat S_{t+1}\right)
-\frac{R}{R^{\$}-R^d}\left(\psi_t-\psi\right),
\qquad
\eta\equiv\frac{R^d}{R^{\$}-R^d}.
\tag{5'}
$$

At a liquidity premium of one to two percent, $\eta$ is of order 50 to 100:
an expected appreciation of one percent roughly doubles foreign deposit
demand. This response generates the deposit outflow and CNH discount after a
transitory endowment shock.

Under passive policy, the impact signatures
$(\widehat S_t,\widehat E_t)$ are

1. $n_t>0$: $(-,+)$. A dollar inflow appreciates CNY and deposit expansion
   produces a CNH premium.
2. $\mu_t>0$: $(+,+)$. Household deposit demand produces the same premium but
   crowds out foreign deposits, causing CNY depreciation.
3. $y_{T,t}>0$: $(-,-)$. The windfall appreciates CNY and reduces foreign CNH
   deposits, which raises $x_t$ and produces a CNH discount via (3). By
   (5'), a transitory shock reduces foreign deposits because the expected
   reversal of the appreciation lowers their return. For a persistent shock,
   foreign investors keep a constant dollar value of CNH, so the appreciation
   lowers their CNH balances. The discount is present at every $\rho$.

These signs distinguish isolated shocks, not arbitrary simultaneous
innovations. In the linearized model, the two prices are a $2\times3$ map from
$(\varepsilon^y_t,\varepsilon^n_t,\varepsilon^\mu_t)$. The basis identifies
$x_t$, but not the decomposition of deposits by funding source. The onshore
rate supplies a second signal through the balance of payments, but it also
mixes fundamentals with financial flows. The map is rank-deficient.

For example, combine $y_{T,t}>0$ with $\mu_t>0$. The endowment
shock causes appreciation and a discount; household deposit demand causes
depreciation and a premium. For suitable magnitudes, the net observation is an
appreciation with a premium, exactly the signature of $n_t>0$. A rule that
interprets this pair as a noise inflow purchases the inferred dollar flow in
full. The correct full-information response is only the saved share
$s(\rho)\varepsilon^y_t$, less the small revaluation outflow, for the
endowment component and zero for the $\mu_t$ component. **This misinterpretation therefore over-accumulates reserves and
resists an appreciation that should partly have been accommodated.**

# 4. From delayed intervention to daily fixing responsiveness

The model's delayed policy rules condition on yesterday's basis and, for the
foreign exchange leg, yesterday's onshore rate:

$$
\begin{aligned}
\Delta M_t
  &=\alpha_{ME}\widehat E_{t-1}
    -\bar D\bigl(\mathbb E_{t-2}^P[x_{t-1}]-\bar x\bigr),
\qquad
\alpha_{ME}=\bar D\frac{\beta}{\phi_{xx}},\\
\Delta B^{\$}_t
  &=-\alpha_B\widehat S_{t-1}
    +\alpha_{BE}\widehat E_{t-1},
\end{aligned} \tag{12}
$$

with $\bar D$ steady-state deposits. The first equation is the linearized
offset $M_t=\bar x\,D_{t-1}$ of Section 1.2 and uses only the basis. The
deviation it removes has two parts:

$$
x_{t-1}-\bar x
=\bigl(x_{t-1}-\mathbb E_{t-2}^P[x_{t-1}]\bigr)
+\bigl(\mathbb E_{t-2}^P[x_{t-1}]-\bar x\bigr).
$$

The first is the surprise, which $\widehat E_{t-1}$ measures through (4) and
$\alpha_{ME}$ converts back into CNH. The second is the drift that was
forecast at the start of $t-1$ and that $\lambda_{t-1}$ accommodated by
placing the parity point at $\mathbb E_{t-2}^P[x_{t-1}]$; it is known to the
policymaker and does not appear in the basis. Thus $\lambda$ handles the
drift ex ante and $M$ removes it ex post, together with the surprise. The
onshore rate does not enter: it would help only if $M$ were accommodating
expected deposits, which is the role assigned to $\lambda$. In the second
equation, the basis helps to infer whether a lagged CNY movement should be
resisted via $\Delta B^{\$}$. For the same CNY appreciation, a CNH premium points toward a financial inflow,
whereas a CNH discount points toward a fundamental improvement. The
rule should therefore resist the first appreciation more strongly than the
second. 

Rule (12) is a simple linear policy in the two observed prices. It
is neither the Bayesian rule that would set the reserve stock to its
posterior-expected target nor a welfare-optimal rule; the choice of
$(\alpha_B,\alpha_{BE})$ is taken as given and their optimality is outside
the scope of this note.

## 4.1 Connect to daily fixing

To connect (12) to the daily fixing, nominal objects outside the model are
needed. Let $E^{\$}_t$ denote the nominal onshore exchange rate in CNY per
dollar, $P_t$ the onshore consumer price index, and $P^*_t$ the US consumer
price index. The model's $S_t$ corresponds in the data to

$$
S_t\simeq E^{\$}_t\frac{P^*_t}{P_t}, \tag{13}
$$

with the approximation reflecting the difference between consumer price
indices and the model's nontradable and tradable prices. In the model
$P_t=P^*_t=1$ by the numeraire choices of Section 1.1, so $S_t$ and $E^{\$}_t$
coincide there; in the data they differ by the relative price level.

Let $e^m_t\equiv\log E^{\$,m}_t$ be the log market close and
$f_t\equiv\log E^{\$,fix}_t$ the log official fixing, both nominal CNY per
dollar. Here $\Delta e^m_{t-1}=e^m_{t-1}-e^m_{t-2}$ is the change between
the two preceding onshore market closes. Define fixing pass-through as the
response of today's official fixing change, $\Delta f_t$, to that previous
trading-day market movement:

$$
\Delta f_t
=a_r+\chi_r\Delta e^m_{t-1}
 +\delta_r\widehat E_{t-1}
 +\eta_r\Delta e^m_{t-1}\widehat E_{t-1}
 +\pi_r(\pi_{CN,t}-\pi_{US,t})
 +u_t, \tag{14}
$$

where $r$ indexes the policy regime and
$\pi_{CN,t}-\pi_{US,t}$ is the onshore-US inflation differential. 

Equation (14) also gives the main empirical implication of the basis signal.
The marginal pass-through is

$$
\frac{\partial\Delta f_t}
     {\partial\Delta e^m_{t-1}}
=\chi_r+\eta_r\widehat E_{t-1}. \tag{15}
$$

If a CNH premium makes the authority resist a given market movement more,
pass-through should fall with the basis, so $\eta_r<0$ under these sign
conventions. A significant interaction means that the fixing treats the same
CNY-USD movement differently depending on offshore liquidity conditions.

## 4.2 Interpretation
Equation (14) provides a reduced-form test of whether the basis predicts
state-dependent fixing pass-through. Its coefficients cannot be interpreted
directly as the structural policy-rule coefficients in (12), for three
reasons.

First, equation (12) uses the quantity $B^{\$}_t$, whereas the fixing is a
price. A stronger reserve response generally corresponds to a larger fixing
offset, but the coefficient $\alpha_B$ is not equal to $1-\chi_r$. Market
depth, expectations, and the implementation of intervention lie between the
two.

Second, the model rate $S_t$ is a relative price of goods, while the
observed market rate $E^{\$,m}_t$ and fixing $E^{\$,fix}_t$ are nominal. From
(13), with $\pi_{CN,t}$ and $\pi_{US,t}$ the onshore and US inflation rates,

$$
\Delta\log S_t
\simeq\Delta\log E^{\$}_t
 -\left(\pi_{CN,t}-\pi_{US,t}\right). \tag{16}
$$

At daily frequency, the price levels are plausibly predetermined, so nominal
and real movements are approximately the same. Across multi-year regimes,
part of the real adjustment to persistent fundamentals can occur through the
inflation differential. For example, suppose equilibrium requires a 3% real appreciation. With zero inflation differential, the nominal
exchange rate must appreciate by 3%. If Chinese inflation exceeds
US inflation by 2%, a 1% nominal appreciation produces the
same real adjustment. A regime comparison that omits the inflation
differential could misread the smaller nominal movement as stronger
intervention even though real accommodation is unchanged.

Third, the baseline model filters current hidden states but does not learn the
shock variances or persistence parameters endogenously. Across regimes, a
change in the optimal fixing response represents reoptimization after an
exogenous change in perceived shock variances, persistence, objectives, or
constraints. It does not, by itself, describe how the authority learned those
distributions.

The regression interpretation uses the hierarchical specification in (14):
the market-change and basis main effects are included together with their
interaction. If the across-regime difference in $\chi_r$ becomes smaller
after adding the basis main effect and its interaction with the market movement,
the evidence is consistent with a change in the information set or in how the
basis was used. If the regime difference remains, it is more consistent with
different shock distributions, objectives, or constraints. The regression is
still reduced form because the fixing is not the model's policy instrument.

# 5. Why temporary information errors have persistent real effects

The model separates the duration of an information error from the duration of
its allocation effect. The former is governed by filtering and data arrival.
The latter is governed by stocks.

Three paths are compared. $P$ is the baseline: delayed instruments, prices
only. $O$ is the oracle delayed policy of Section 1.2: the same delayed
instruments, but the date-$t$ shock decomposition is known once markets
clear. $FI$ is the contemporaneous allocation of Section 2. For any variable
$z$,

$$
\underbrace{z^P-z^{FI}}_{\text{total allocation gap}}
=\underbrace{(z^P-z^O)}_{\text{incomplete information}}
+\underbrace{(z^O-z^{FI})}_{\text{implementation delay}}.
$$

Sections 5.1--5.3 study the first component; Section 5.4 adds the second and
shows that the persistent part of the total gap comes entirely from the
first.

## 5.1 The information error itself does not compound

After the date-$t$ markets clear, the policymaker observes $(S_t,E_t)$ and
knows its own settings $(M_t,B^{\$}_t,\lambda_t)$. Given the known
$\lambda_t$, equation (3) makes $E_t$ an exact signal of $x_t$, and hence of
$D_t=M_t/x_t$; the onshore rate supplies a second signal through the balance
of payments. From $(x_t,S_t,E_t)$ and the shock dynamics in (1), the
policymaker forms a posterior over $(y_{T,t},\mu_t,n_t)$. By Section 3, the
posterior mean differs from the realized decomposition whenever the price
pair is consistent with more than one shock mix. The date-$(t+1)$ settings then inherit the error: $\lambda_{t+1}$
through the posterior and $B^{\$}_{t+1}$ through the rule's reading of the
same price pair, as Sections 5.2 and 5.3 detail. $M_{t+1}=\bar x\,D_t$ uses
only $x_t$, which is known exactly, and is therefore identical under $P$ and
$O$.

The error does not compound. Compounding would require that the price effect
of a mistaken setting be indistinguishable from a new shock, so that one
misclassification induces another. It is not. All three date-$(t+1)$ settings
belong to $\mathcal I^P_t$, and private agents share that information set.
Conditional on the known settings, the equilibrium map from the date-$(t+1)$
innovations to $(S_{t+1},E_{t+1})$ is known, so the new price pair remains a
function of the new innovations alone. A policy error shifts a known reference
point; it does not add an unobserved measurement error to the filter.

The liquidity setting is the critical instance. $\lambda_{t+1}$ is the only
setting that depends on the posterior, and it enters the signal equation (3)
directly, so it is the one channel through which a posterior error could
contaminate the next signal. Yet $\lambda_{t+1}$ is known at $t+1$ even if it
was chosen from a mistaken forecast, so (3) still inverts to recover $x_{t+1}$
exactly from $E_{t+1}$. With stationary AR(1) shocks, the filtering error
therefore has finite variance and decays as new prices arrive. A highly
persistent shock can make the filter converge slowly, but the error does not
feed back into an ever-worsening signal. What persists is not the information
error but the stocks that the mistaken settings create, to which the next two
subsections turn.

## 5.2 Foreign exchange leg: a stock error with a permanent-income effect

The yardstick for the FX leg is the flow that a correctly informed
policymaker would choose under the same delay.

**The oracle flow.** Consider a shock that arrives at $t$ when the economy is
at its steady state, so that reserves are on the oracle path,
$B^{\$}_{t-1}=B^{\$,O}_{t-1}$, no reserve change was expected at $t$, and
the date-$t$ settings are predetermined, so that no policy responds to the
shock at $t$. Full information would have purchased, at $t$, the amount
prescribed by (8) and (11),

$$
\Delta B^{\$,FI}_t
=\frac{E_tn_t}{S_t}
+s(\rho)\varepsilon^y_t
-\theta_0\Delta c_{T,t}.
$$

The oracle, which learns the decomposition only after date-$t$ markets clear,
implements this at $t+1$. Its purchase is

$$
\Delta B^{\$,O}_{t+1}
=\underbrace{R^{\$}\,\Delta B^{\$,FI}_t}_{\text{missed date-}t\text{ purchase, carried at }R^{\$}}
+\underbrace{\mathbb E_t^O\!\left[\Delta B^{\$,FI}_{t+1}\right]}_{\text{expected date-}(t+1)\text{ purchase}}.
$$

The first term closes the gap opened by the one-period delay. The second is
the predictable date-$(t+1)$ change in the desired reserve stock: a noise
position unwinds as $(\rho_n-1)E_tn_t/S_t<0$, while an AR(1) endowment
requires continued saving as income remains above its new permanent level.
The shock $\mu_t$ contributes to neither term because it brings no dollar
flow. The two terms sum to the oracle's date-$(t+1)$ purchase by
construction. Because the steady state was the starting point, the missed
purchase is also the date-$t$ innovation in the full-information flow; Section
5.4 uses that innovation for the general case.

**The baseline flow.** The baseline FX leg is the fixed linear rule (12),
$\Delta B^{\$}_{t+1}=-\alpha_B\widehat S_t+\alpha_{BE}\widehat E_t$, a
fixed-coefficient rule in the two observed prices that is not re-optimized
against the inherited stock gap. Even when calibrated to reproduce the oracle
flow, the price rule generally differs from $\Delta B^{\$,O}_{t+1}$ because
the same observed price pair can be generated by multiple shock combinations.

**From a flow error to a persistent stock gap.** Define

$$
q_t^B\equiv B^{\$}_t-B^{\$\,O}_t
$$

as the reserve-stock error, with $q_t^B>0$ denoting an excess dollar
purchase. Neither the baseline rule nor the oracle flow responds to the
inherited gap $q_t^B$. Once their flow difference returns to zero, the stock
gap therefore remains constant at $q_t^B$, and by (7) the interest
$(R^{\$}-1)q_t^B$ on it is spent through the current account in every later
period. Learning that the purchase was mistaken does not sell the asset
because rule (12) contains no stock-gap feedback.

This persistence is a property of the flow rule, not of incomplete
information itself. A posterior stock rule
$B^{\$}_{t+1}=\mathbb E_t^P[B^{\$,O}_{t+1}]$ would re-estimate past shocks
as the posterior updates and partially reverse earlier purchases. Under
(12), only an explicit reversal of $q_t^B$ removes the stock effect.

To isolate its allocation effects, suppose the liquidity leg continues to
stabilize $x_t$, so $\psi_t$ and the basis remain on their oracle delayed
paths. Under this assumption the deviations derived below, written with
superscript $O$, measure the FX component of the gap from the oracle: the
difference between an informed path that inherits the baseline reserve
choice and the oracle itself. Section 5.3 adds the liquidity component and
Section 5.4 combines them.

**Impact and transition.** Consider a combination
$(y_{T,t}>0,\mu_t>0)$ that produces a CNY appreciation with a CNH premium.
The policymaker misclassifies this price pair as a noise-trader inflow,
$n_t>0$. Rule (12),
when calibrated to the noise response, buys the dollars needed to absorb the
inferred inflow, whereas the oracle saves only
$s(\rho)\varepsilon_t^y$ against the fundamental shock. The baseline
purchase therefore exceeds the oracle purchase when full-information saving
against fundamentals falls short of full absorption of the inferred inflow.
The resulting $q_t^B>0$ is a condition on the rule calibration.

Given this excess purchase, financing must come from lower tradable
consumption, a foreign deposit inflow, or both. The two are linked through
(5): lower current consumption with an expected recovery is an expected CNY
appreciation, and by $(5')$ foreign deposits respond with elasticity $\eta$.
Solving the country budget (9) jointly with foreign deposit demand, while
holding $x_t$ fixed, shows that consumption absorbs only a fraction of
$q_t^B$ on impact. At
$\eta\,d^f/(\iota y_{NT})$ of order 50, it is about one eighth; the remainder
is financed by a foreign CNH inflow, which the liquidity leg must match with
$x\,\Delta d^f_t$ to keep $x_t$ fixed. The foreign deposit inflow subsequently unwinds as expected appreciation vanishes; consumption recovers from its initial shortfall and eventually settles above the oracle path by approximately $(R^{\$}-1)q_t^B$, for reasons explained next.

**Permanent-income effect.** Acquiring the additional dollar asset requires lower consumption
on impact or temporary financing through foreign CNH deposits. Although that
financing unwinds, the additional reserve asset remains if the rule never
reverses the purchase. Dollar net foreign assets are therefore permanently
higher by $q_t^B$.

The additional asset then pays
$R^{\$}q_t^B$ next period. Maintaining the principal $q_t^B$ leaves net
interest $(R^{\$}-1)q_t^B$ available for consumption in every subsequent
period, whose present value is

$$
\sum_{j=1}^{\infty}
\frac{(R^{\$}-1)q_t^B}{(R^{\$})^j}
=q_t^B.
$$

This annuity exactly offsets the initial and transitional shortfall of consumption, as the country budget requires the
present value of the initial and transitional consumption shortfall to equal
the present value of the later consumption surplus, up to terms of order
$\theta$:

$$
\sum_{j\geq0}\frac{e^c_{t+j}}{(R^{\$})^{j}}\simeq0,
\qquad
e^c_{t+j}\equiv c_{T,t+j}-c^O_{T,t+j}. \tag{17}
$$

Therefore, once the financing transition is complete, consumption approaches
the net interest on the additional asset:

$$
e^c_{t+j}
\to(R^{\$}-1)\,q_t^B
\qquad\text{as } j\to\infty, \tag{18}
$$

again up to terms of order $\theta$. The error thus forces additional saving:
it shifts consumption from the transition into the future rather than
improving the allocation for free. Relative to the oracle's smoother path,
this timing distortion generally lowers welfare under concave utility even
though long-run consumption is higher. If $q_t^B<0$, all signs reverse:
consumption is initially higher but permanently lower.

**Exchange-rate implication.** By (2), the associated real exchange rate
error is approximately

$$
e^S_{t+j}
\equiv
\log S_{t+j}-\log S^O_{t+j}
\simeq
-\frac{e^c_{t+j}}{\bar c_T}. \tag{19}
$$

An excess purchase therefore causes a real depreciation during the transition
and a persistent appreciation thereafter, relative to the oracle delayed
path.

**Why holding $x_t$ fixed is innocuous for the FX leg.** The derivation above
isolates the allocation effect of the reserve-stock error by holding the
reserve-deposit ratio $x_t$ fixed. At first sight, this seems inconsistent
with the baseline equilibrium. An excess purchase depresses current
consumption with an expected recovery, so by $(5')$ the expected appreciation
attracts foreign deposits with elasticity $\eta$. Because
$M_t=\bar xD_{t-1}$ is predetermined, the resulting increase in $D_t$ lowers
$x_t=M_t/D_t$. Thus $M_t$ does not deliver the fixed-$x_t$ assumption.

What makes that assumption innocuous for the FX leg is $\lambda_t$, which
neutralizes the predictable effect of the movement in $x_t$ on the basis.
The policymaker chooses $\Delta B^{\$}_t$ and
$\lambda_t=\Lambda(\mathbb E^P_{t-1}[x_t])$ together, while
$\mathbb E^P_{t-1}[D_t]$ already includes the foreign deposit response in
$(5')$ to the consumption path that the purchase implies. Hence $\lambda_t$ absorbs the expected
deposit effect of the purchase whether or not the purchase matches the
oracle's. Holding $x_t$ fixed therefore leaves the anticipated liquidity
channel out of the FX-leg derivation without changing its conclusion. Only
the unanticipated deposit component remains in the basis:

$$
\widehat E_t\;\propto\;-\bigl(D_t-\mathbb E^P_{t-1}[D_t]\bigr),
$$

which by (3) is a CNH premium when $D_t$ exceeds its posterior mean. Under an
excess purchase, this surprise reflects the misclassified shock: realized
$S_t$, and therefore $d^f_t$, differ from their posterior forecasts. Rule
(12)'s response through $+\alpha_{BE}\widehat E_t$ is thus delayed learning
about the shock, not a policy echo.

Two remarks complete the picture. A contemporaneous
$\Delta M_t=x\,\Delta d_t^f$ that held $x_t$ fixed is infeasible on both
counts of Section 1.2: $M_t$ is predetermined, and the policymaker learns
$D_t$ only after $E_t$ reveals $x_t$, without separating $d^f_t$ from the
other components. And the stock error $q_t^B$ is undone only by a rule that
reverses past purchases, which (12) does not.

## 5.3 Liquidity leg: a forecast wedge with a persistent stock effect

**Forecast error and comparison benchmark.** To isolate the liquidity error
from the FX-leg error, compare the policymaker with a counterfactual
$O\mid B$. This benchmark knows the date-$t$ shock decomposition but inherits
the policymaker's $B^{\$}_{t+1}$; from $t+2$ onward it applies the same policy
rules to the prices generated by its own liquidity setting. Unlike the full
delayed oracle $O$, therefore, $O\mid B$ corrects the liquidity forecast
without replacing the current FX choice. Holding that choice fixed removes
the initial reserve-stock error $q_t^B$, whose allocation effects are already
counted in (18) and (19).

The remaining difference begins with the forecast of deposit drift. At $t$,
$D_t$ is known, but its components evolve differently. Section 1.2 gives

$$
\lambda_{t+1}
=\Lambda\!\left(\mathbb E_t^P[x_{t+1}]\right),
\qquad
\mathbb E_t^P[x_{t+1}]
=\frac{\bar xD_t}{\mathbb E_t^P[D_{t+1}]},
$$

where

$$
D_{t+1}
=\frac{\mu_{t+1}}{E_{t+1}\psi_{t+1}}
+d^f_{t+1}+n_{t+1},
\qquad
n_{t+1}=\rho_nn_t+\varepsilon^n_{t+1},
$$

and $d^f_{t+1}$ satisfies (5). For example, interpreting
$(y_{T,t}>0,\mu_t>0)$ as $n_t>0$ makes the policymaker forecast reversion at
$\rho_n$ rather than at the true persistence $\rho$ and $\rho_\mu$.

Under the same inherited FX choice, the informed benchmark sets

$$
\mathbb E_t^{O\mid B}[x_{t+1}]
\equiv\mathbb E_t^O[x_{t+1}\mid B^{\$}_{t+1}],
\qquad
\lambda^{O\mid B}_{t+1}
\equiv\Lambda\!\left(\mathbb E_t^{O\mid B}[x_{t+1}]\right).
$$

The forecast and policy errors are therefore

$$
h_t
\equiv
\mathbb E_t^P[x_{t+1}]
-\mathbb E_t^{O\mid B}[x_{t+1}],
\qquad
e^{\lambda\mid B}_{t+1}
\equiv\lambda_{t+1}-\lambda^{O\mid B}_{t+1}
\simeq\Lambda'h_t. \tag{20}
$$

Thus $h_t$ measures only the drift misforecast and its subsequent equilibrium
effects.

**Impact and transition.** Treat
$e^{\lambda\mid B}_{t+1}=\Lambda'h_t$ as the policy error. It affects $\psi$
through two channels: directly through $\lambda_{t+1}$ and indirectly through
$x_{t+1}=M_{t+1}/D_{t+1}$, which moves as deposit demand changes while
$M_{t+1}$ is predetermined. Define
$e^{x\mid B}_{t+1}=x_{t+1}-x^{O\mid B}_{t+1}$ and
$e^{\psi\mid B}_{t+1}=\psi_{t+1}-\psi^{O\mid B}_{t+1}$. Their joint effect
on the intermediation cost is

$$
e^{\psi\mid B}_{t+1}
\simeq
\psi_xe^{x\mid B}_{t+1}
+\psi_\lambda e^{\lambda\mid B}_{t+1}
=\psi_xe^{x\mid B}_{t+1}
+\psi_\lambda\Lambda'h_t. \tag{21}
$$

Consider the example above when the true shocks are more persistent than the
noise position, $\rho,\rho_\mu>\rho_n$. The policymaker then forecasts too
little deposit demand at $t+1$, overpredicts $x_{t+1}$, and sets
$\lambda_{t+1}$ too high, so $h_t>0$. Relative to $O\mid B$, liquidity at
$t+1$ is too tight for the deposits actually in place. 

The initial impact
is twofold. First, realized $x_{t+1}$ lies below the value embedded in
$\lambda_{t+1}$, so (3) produces a CNH premium. Second, since
$\psi_\lambda>0$, the marginal cost of supplying deposits is higher, which
by (21) raises $\psi_{t+1}$ and lowers the promised return $R^d_{t+1}$.

Both effects reduce deposit demand and reinforce each other. The CNH
premium raises the dollar price $E_{t+1}/S_{t+1}$ of a CNH claim and,
holding its payoff fixed, lowers its expected dollar return; the higher
$\psi_{t+1}$ lowers the payoff itself. In $(5')$ both terms push foreign
CNH demand down, and the higher CNY cost $E_{t+1}\psi_{t+1}$ of liquidity
services lowers household demand as well.

Two channels mitigate the outflow. The first works through the ratio
itself: the fall in $D_{t+1}$ raises $x_{t+1}$, which by (3) narrows the
premium and by $\psi_x<0$ pulls $\psi_{t+1}$ back toward its $O\mid B$
value. This channel cannot overshoot, because an $x_{t+1}$ high enough to
reverse the premium and the cost increase would also reverse the outflow
that produced it. The second works through the exchange rate. Foreigners
who leave CNH deposits take dollars, and with $B^{\$}_{t+1}$
predetermined these dollars come from the current account, so $S_{t+1}$
depreciates. A temporary depreciation creates an expected appreciation,
which enters $(5')$ with the large elasticity $\eta$: a small depreciation
is enough to re-attract deposits, so the exchange rate absorbs much of the
pressure and the quantity response is dampened.

The net outcome at $t+1$ is a CNH premium, a higher $\psi_{t+1}$, a lower
$d^f_{t+1}$, and a weaker CNY, with the partial offsets leaving $x_{t+1}$
below and $\psi_{t+1}$ above their $O\mid B$ values. 
If $h_t<0$, every sign reverses: liquidity is too loose, producing a CNH
discount, a lower $\psi_{t+1}$, a foreign deposit inflow, and CNY
appreciation pressure.

**From the wedge to an external-asset flow.** In the example, the
foreign deposit outflow at $t+1$ is an external-asset flow. Foreigners who
leave CNH deposits are paid in dollars, and since $B^{\$}_{t+1}$ is the
same on both paths, (7) requires these dollars to come from a larger trade
surplus at $t+1$. The country therefore retires foreign liabilities it
would have kept under $O\mid B$. The impact NFA difference comes entirely
from the foreign CNH liability and its valuation:

$$
e^{A\mid B,t}_{t+1}
=-
\left[
\frac{E_{t+1}(d^f_{t+1}+n_{t+1})}{S_{t+1}}
-
\frac{E^{O\mid B}_{t+1}
      (d^{f,O\mid B}_{t+1}+n_{t+1})}
     {S^{O\mid B}_{t+1}}
\right].
$$

The lower $d^f_{t+1}$ raises NFA relative to $O\mid B$. The valuation
term has no definite sign: the CNH premium raises, while the CNY
depreciation lowers, the dollar value $E_{t+1}/S_{t+1}$ of each remaining
CNH liability, including the noise position $n_{t+1}$, which is common to
both paths. The quantity effect is therefore the unambiguous part of the
impact flow. Reserves cannot adjust while date-$(t+1)$ markets clear.
After they close, rule (12) observes the distorted $(S_{t+1},E_{t+1})$
and chooses a different $B^{\$}_{t+2}$. A temporary liquidity wedge thus
changes current NFA through the liability outflow and its valuation, and
future reserves through the policy response. For $h_t<0$ the signs
reverse: a deposit inflow financed by a trade deficit lowers NFA.

**From a temporary wedge to a persistent stock gap.** For the
misclassification originating at $t$, let $\tau_t$ be the first date at which
the posterior drift forecast has caught up with $O\mid B$ and the direct
liquidity wedge has closed. Closing the wedge eliminates the forecast error, but
does not automatically reverse the NFA and reserve flows accumulated while
it was active; rule (12) targets neither inherited gap.

Write $e^{A\mid B,t}_s$ for the date-$s$ NFA deviation attributable to this
episode, holding other innovations fixed, and let $A_s^{O\mid B,t}$ denote
the corresponding informed counterfactual. The residual stock at closure is

$$
q_t^L
\equiv e^{A\mid B,t}_{\tau_t}
=A_{\tau_t}-A_{\tau_t}^{O\mid B,t}.
$$

Since the two paths share $A_t$, $q_t^L$ is the gap in cumulative net
external asset accumulation over $t+1,\dots,\tau_t$. By (7), it is the
cumulative difference in the current account plus the cumulative
revaluation of CNH liabilities, with the FX reserve stocks $B^{\$}_s$
allowed to differ from $s=t+2$ once rule (12) reacts to different prices.
In the example, each
period while the wedge is active repeats the impact pattern: fewer foreign
CNH liabilities financed through the trade balance, together with a
valuation term of indeterminate sign. If the forecast catches up at
$t+1$, so that $\tau_t=t+1$, then $q_t^L=e^{A\mid B,t}_{t+1}$ and the
impact expression above is the entire stock effect.

**Permanent-income effect.** The same permanent-income logic as in (18) then gives

$$
e^{c,L,t}_{\tau_t+j}
\equiv c_{T,\tau_t+j}-c^{O\mid B,t}_{T,\tau_t+j}
\to(R^{\$}-1)q_t^L
\qquad\text{as }j\to\infty. \tag{22}
$$

Thus a temporary liquidity forecast error can have a permanent consumption
effect. With repeated errors, the aggregate liquidity-induced NFA stock
contains the $q_t^L$ contributions of completed episodes together with the
current NFA deviations of episodes whose wedges remain active.

**Exchange-rate implication.** 
(2) maps the consumption effect into the real exchange rate:

$$
e^{S,L,t}_{\tau_t+j}
\equiv
\log S_{\tau_t+j}-\log S^{O\mid B,t}_{\tau_t+j}
\simeq
-\frac{e^{c,L,t}_{\tau_t+j}}{\bar c_T}
\to
-\frac{(R^{\$}-1)q_t^L}{\bar c_T}.
$$

A positive $q_t^L$ therefore produces a persistent real appreciation after
the transition; a negative $q_t^L$ produces a persistent depreciation. In
the example, $h_t>0$ forces a foreign deposit outflow that is financed
through the trade balance, so $q_t^L>0$ when the quantity effect dominates
the valuation term. The weaker CNY during the transition then gives way to
a persistent real appreciation once the wedge has closed. For $h_t<0$ both
phases reverse.

For this mistake, $q_t^B>0$ under the calibration condition in Section 5.2,
and $q_t^L>0$ when the quantity effect dominates the valuation term. Under
these sign conditions, the two legs have the same two-phase allocation
effect. During the impact and transition, external-asset accumulation lowers
current consumption and produces a real depreciation relative to the
informed path. As the temporary financing and liquidity distortions unwind,
consumption recovers and the real exchange rate appreciates. Because the
additional asset stocks remain, long-run consumption is higher by the
interest they earn and the real appreciation is persistent.

The mechanisms differ. The FX error $q_t^B$ is a mistaken reserve purchase
made at once and then inherited because rule (12) has no stock-gap feedback.
The liquidity error $q_t^L$ accumulates through deposit outflows, the trade
balance, valuation, and subsequent reserve responses while the wedge is
active; during that interval it also moves the basis and $\psi$. The
$O\mid B$ comparison holds the initial FX choice fixed, isolating this second
mechanism without counting the reserve-purchase error again.

## 5.4 From information errors to the total allocation gap

The total gap from the contemporaneous full-information
allocation, $FI$, has two components. Sections 5.1--5.3 study the information
component, $P-O$: the policymaker and the delayed oracle face the same timing,
but only the oracle knows the shock decomposition. The remaining component,
$O-FI$, is the cost of implementing the correct response one period late.
The first component can leave the stock errors $q_t^B+q_t^L$ and hence a
persistent allocation gap. The second shifts reserves and consumption across
adjacent dates but has no permanent component.

**The implementation-delay component.** The oracle implements the $FI$
response to each shock with a one-period delay. This delay affects reserve
timing, the intervening allocation, and liquidity in sequence.

**Reserve timing.** The oracle implements the predictable part of the $FI$
reserve flow but cannot respond to its date-$t$ innovation until $t+1$.
Define that innovation by

$$
u_t^B
\equiv
\Delta B_t^{\$,FI}
-\mathbb E_{t-1}^O[\Delta B_t^{\$,FI}].
$$

After the previous innovation has been corrected, the date-$t$ reserve gap is
therefore

$$
B_t^{\$,O}-B_t^{\$,FI}=-u_t^B.
$$

At $t+1$, the oracle's reserve purchase includes

$$
R^{\$}u_t^B
+\mathbb E_t^O[\Delta B_{t+1}^{\$,FI}].
$$

The first term closes the date-$t$ gap with interest, while the second
implements the predictable continuation of the $FI$ flow. The newly realized
date-$(t+1)$ innovation $u_{t+1}^B$ then leaves
$B_{t+1}^{\$,O}-B_{t+1}^{\$,FI}=-u_{t+1}^B$, which the oracle corrects at
$t+2$. The implementation-delay component is therefore a rolling
one-period innovation rather than a permanent reserve gap.

**Allocation during the delay.** The innovation $u_t^B$ is the reserve flow
that $FI$ undertakes at $t$ but the oracle postpones until $t+1$. Suppose
$u_t^B>0$: $FI$ would buy additional dollars, whereas the oracle
temporarily under-purchases them. The resulting excess dollar supply makes
the CNY appreciate, so $S_t$ falls. This appreciation helps equation (7)
clear in two ways. First, it raises tradable consumption through
$c_{T,t}=\iota y_{NT}/S_t$ and thereby reduces the trade balance. Second,
with the liquidity wedge accommodated, (5) gives $d_t^f=\kappa S_t$, so
$d_t^f$ also falls. Foreign investors therefore redeem part of their CNH
deposits for dollars, offsetting part of the excess dollar supply. Equations
(5) and (7) jointly determine how much adjustment occurs through this
foreign-liability flow and how much through $S_t$.

A positive noise inflow provides one example: it creates a dollar inflow that
$FI$ would absorb with reserves, so delayed intervention gives $u_t^B>0$,
$S_t\downarrow$, $d_t^f\downarrow$, and $c_{T,t}\uparrow$. A positive
endowment innovation has the same signs for the amount
$s(\rho)\varepsilon_t^y$ that $FI$ would save. At $t+1$, the oracle's
purchase $R^{\$}u_t^B$ raises dollar demand and closes the inherited reserve
gap. After the associated CNH financing unwinds, this innovation leaves no
permanent asset principal. Applying the annuity logic following (17) with a
zero terminal asset gap therefore gives
$c^O_{T,t+j}-c^{FI}_{T,t+j}\to0$. All signs reverse when $u_t^B<0$. A new
$u_{t+1}^B$ may create the next one-period deviation, but it does not prolong
the effect of $u_t^B$.

**Liquidity during the delay.** Under $FI$, contemporaneous adjustment of
$M_t$ keeps $x_t=\bar x$ and $\lambda_t=\bar\lambda$. Under the oracle,
$M_t=\bar xD_{t-1}$ does not absorb the change from $D_{t-1}$ to $D_t$, so
$x_t$ deviates from $\bar x$. This changes $\psi_t$ and therefore $R^d_t$,
$d^h_t$, and $d^f_t$ through (5) and (6), as well as the realized basis.
The predetermined setting
$\lambda^O_t=\Lambda(\mathbb E^O_{t-1}[x_t])$ shifts the expected parity
point to offset the predictable part of the basis movement. It neither
restores $x_t$ nor prevents the quantity responses; the unexpected part of
the deviation in $x_t$ appears in $\widehat E^O_t$.

At $t+1$, $M_{t+1}=\bar xD_t$ catches up with the observed date-$t$ deposit
stock and removes the gap in $x$ caused by using $D_{t-1}$ rather than
$D_t$. It does not guarantee $x_{t+1}=\bar x$: a continuation or new change
from $D_t$ to $D_{t+1}$ creates the next gap, while $\lambda^O_{t+1}$ offsets
only its predictable basis effect. The update also cannot undo the date-$t$
allocation cost: households and foreigners chose $d^h_t$ and $d^f_t$ at the
wrong $\psi_t$, and banks incurred the corresponding intermediation cost.
It also leaves a repayment difference at $t+1$:

$$
\Delta\mathcal R^{O-FI}_{t+1}
\equiv
E^O_{t+1}R_t^{d,O}(d_t^{f,O}+n_t)
-
E^{FI}_{t+1}R_t^{d,FI}(d_t^{f,FI}+n_t).
$$

The noise position $n_t$ is identical in the two allocations. The quantity
difference is therefore only $d_t^{f,O}-d_t^{f,FI}$; $n_t$ enters
$\Delta\mathcal R^{O-FI}_{t+1}$ only as a common foreign liability whose
repayment value differs when $R_t^d$ or $E_{t+1}$ differs. This repayment
difference affects the date-$(t+1)$ current account, $S_{t+1}$, and deposit demand.

For the impulse response to an isolated date-$t$ innovation, the
foreign-held deposits mature at $t+1$, while the correctly informed updates
of $M$, $\lambda$, and reserves remove rather than perpetuate the source of
the wedge. Holding subsequent innovations fixed, stable equilibrium dynamics
therefore make the remaining quantity and valuation differences converge to
zero. The round trip leaves no terminal NFA gap up to the order-$\theta$
liquidity-rent and revaluation terms in (9). Unlike $q_t^L$ in Section 5.3,
the correct-information delay thus creates a one-time allocation cost but no
additional stock error. With new deposit changes, this temporary sequence can
repeat without making any one delay permanent.

**Total gap.** Combining,

$$
\begin{aligned}
B^{\$,P}_t-B^{\$,FI}_t
&=\underbrace{(B^{\$,P}_t-B^{\$,O}_t)}_{\text{permanent unless reversed}}
+\underbrace{(B^{\$,O}_t-B^{\$,FI}_t)}_{=-u^B_t,\ \text{one period}},\\
c^P_{T,t}-c^{FI}_{T,t}
&=\underbrace{(c^P_{T,t}-c^O_{T,t})}_{\to(R^{\$}-1)(q_t^B+q_t^L)}
+\underbrace{(c^O_{T,t}-c^{FI}_{T,t})}_{\to0},\\
\lambda^P_t-\bar\lambda
&=\underbrace{(\lambda^P_t-\lambda^{O\mid B}_t)}
              _{\text{forecast wedge, }\simeq\Lambda'h_{t-1}}
+\underbrace{(\lambda^{O\mid B}_t-\lambda^O_t)}
                   _{\text{different FX stance}}
+\underbrace{(\lambda^O_t-\bar\lambda)}
             _{\text{predictable drift}}.
\end{aligned}
$$

The $O-FI$ terms are a rolling sequence of one-period innovations. Each
carries a sunk date-$t$ allocation cost, but none leaves a stock: the
reserve gap is closed with interest at $t+1$, and the consumption gap
converges to zero innovation by innovation. Measuring welfare against $FI$
therefore adds this implementation-delay cost without changing the source of
persistence, which remains the stock errors $q_t^B+q_t^L$ induced by
misclassification. The three-part decomposition of $\lambda$ separately
identifies the forecasting wedge, the accommodation of a different FX
stance, and the oracle's predictable drift.

# 6. Empirical content and limits

The main testable implication is a market-rate and basis interaction in the
fixing rule. For a given lagged CNY appreciation, a CNH premium should predict
a larger offset and less pass-through than a CNH discount. Omitting the basis
produces an omitted-variable bias with a specific structure. If the fixing
offset follows (12) but is regressed on the lagged spot change alone, the
slope is

$$
\hat\beta_S
=\alpha_B-\alpha_{BE}\,
\frac{\mathrm{Cov}(\widehat E_t,\widehat S_t)}{\mathrm{Var}(\widehat S_t)},
$$

and the covariance term depends on how often appreciations in the sample are
financial, arriving with a CNH premium, rather than fundamental, arriving
without one. A subsample dominated by carry-trade inflows yields a larger
estimated offset; one dominated by trade-surplus appreciations yields a
smaller one. The estimated responsiveness therefore shifts with the shock mix
even when the rule is unchanged, so an apparent regime change in fixing
behaviour can be an artifact of the omitted basis.

The second implication concerns persistence. Section 5 predicts that a
misclassified shock leaves a short-lived response in the basis and the
intermediation wedge but a permanent response in the reserve stock, with the
consumption and real exchange rate effects in (18) and (19). The natural
test would construct $q_t^B$ directly by feeding the revealed shock sequence
through the oracle rule, but that requires deposits by residency and
noise-trader positions, which a researcher does not observe. The reserve
stock alone is also uninformative: the oracle accumulates reserves
permanently after endowment shocks, since it saves $s(\rho)\varepsilon^y_t$
and never spends the principal, so $B^{\$}_t$ carries a unit root from
fundamentals as well as from $q_t^B$. What remains feasible with reserves,
the spot rate, and the basis are three indirect tests.

**Flow reversal.** Use the lagged basis to identify reserve purchases made
against a CNH premium. If the premium reflects a true noise inflow, the
oracle purchase is reversed as the position decays; absent new shocks, the
remaining reserve increment after $k$ periods is proportional to
$\rho_n^k$. If the same price pair instead reflects an endowment shock
combined with a household liquidity shock—the misclassification of Sections
3 and 5.2—the appreciation decays at $\rho$ rather than $\rho_n$, so the
spot and basis reversals that would trigger sales under rule (12) are
weaker and slower, and the stock error $q_t^B$ remains. A local projection of cumulative reserve
changes after premium-flagged purchases can therefore estimate the
non-reverting component. To see the interpretation, let $b_i$ be the initial
purchase in premium-flagged event $i$, and let $b_{i,\infty}$ be its
long-horizon contribution to the reserve stock. The model predicts

$$
b_{i,\infty}
=
\begin{cases}
0, & \text{true noise inflow},\\
b_i, & \text{misclassified fundamental appreciation}.
\end{cases}
$$

Consequently,

$$
\widehat m_B
\equiv
\frac{\sum_i b_{i,\infty}}{\sum_i b_i}
$$

estimates the purchase-weighted share of non-reverting purchases. It is an
upper bound on the share of mistakes: after an endowment shock the oracle
itself saves $s(\rho)\varepsilon^y_t$ permanently, so part of a
non-reverting purchase is correct saving rather than error. It equals the
fraction of mistaken events only when the $b_i$ are similar and that correct
component is small. More generally, this interpretation requires true noise
purchases to reverse fully, mistaken purchases not to reverse, and unrelated
subsequent reserve shocks to be controlled in the local projection.

The spot rate provides an additional check. Select events in which a CNY
appreciation and CNH premium coincide with a reserve purchase. For each event,
estimate the two horizon responses

$$
\Delta_k B_t^{\$}\equiv B_{t+k}^{\$}-B_{t-1}^{\$},
\qquad
\Delta_k\log S_t\equiv\log S_{t+k}-\log S_{t-1}.
$$

Under a true noise inflow, $n_t$ decays, the appreciation reverses, and the
authority sells back its initial purchase. Both $\Delta_k\log S_t$ and
$\Delta_k B_t^{\$}$ should therefore return toward zero. Under a
misclassified fundamental appreciation, the appreciation decays only at
$\rho$, so $\Delta_k\log S_t$ remains negative, while the purchase is not
reversed, so
$\Delta_k B_t^{\$}$ remains positive. This joint persistence supports the
mistake interpretation. Neither response identifies a mistake on its own:
fundamental saving can also make reserves persistent, while intervention
itself can slow the reversal of the spot rate.

**Price and trade-balance signature.** Use the same premium-flagged events
and estimate the trade-balance and real-exchange-rate responses after
controlling for observable fundamentals. For a positive non-reverting reserve
increment $q_t^B$, the model predicts two phases. While the excess asset is
being acquired, consumption is depressed, the trade balance is above its
fundamental path, and $\log S$ is also above its fundamental path—a real
depreciation. After the financing transition, the country consumes the
asset's net return: the trade balance is below its fundamental path by
approximately $(R^{\$}-1)q_t^B$, while $\log S$ is below its fundamental
path—a persistent appreciation—by (19). The empirical test is therefore a
sign reversal in both responses around a non-reverting reserve purchase:
surplus to deficit for the trade balance and depreciation to appreciation
for the real exchange rate. All signs reverse for $q_t^B<0$.

This test has low power by itself. If $q_t^B$ is ten percent of GDP and the
dollar rate is three percent, the predicted long-run trade-balance gap is
only 0.3 percent of GDP and can easily be obscured by fundamental shocks.

**One-sided drift.** The flow-reversal test asks whether an intervention is
subsequently undone. It does not ask whether the interventions that remain in
reserves have a common direction. Persistent purchases and sales may offset
across events, leaving no aggregate drift, or one side may dominate. The
one-sided-drift exercise distinguishes these cases by combining persistence
with event frequencies, sizes, and signs.

Because later interventions overlap, estimate average horizon responses
rather than an event-specific long-run residual. Let $I_t^+$ indicate
premium/purchase events and $I_t^-$ indicate discount/sale events, and define
their nonnegative intervention magnitudes by

$$
b_t^+\equiv I_t^+\Delta B_t^{\$},
\qquad
b_t^-\equiv-I_t^-\Delta B_t^{\$}.
$$

Separating $b_t^+$ and $b_t^-$ allows purchases and sales to have different
reversal rates. Defining both magnitudes as nonnegative, while subtracting the
sale term in the regression below, gives their coefficients the same
interpretation.

Use the cumulative reserve response

$$
Y^B_{t,k}
\equiv
B_{t+k}^{\$}-B_{t-1}^{\$},
$$

which equals $\Delta B^{\$}_t$ when $k=0$. Because reserve interest is spent
rather than accumulated, a non-reverting intervention leaves the stock, not a
compounding balance; interest on the pre-existing stock is absorbed by the
lagged reserve controls in $X_t$. For each horizon
$k=0,\ldots,K$, estimate a separate regression that jointly pools all
eligible premium/purchase and discount/sale events:

$$
Y^B_{t,k}
=\alpha_k
+\beta_{+,k}b_t^+
-\beta_{-,k}b_t^-
+\Gamma_k'X_t
+u_{t,k},
$$

where $X_t$ contains lags of reserve flows, spot changes, the basis, and
observable macroeconomic fundamentals. The sequences
$\{\beta_{+,k}\}_{k=0}^K$ and $\{\beta_{-,k}\}_{k=0}^K$ are the pooled
average response paths per unit of initial purchase and sale. At a horizon
$K$ long enough that $\rho_n^K$ is small, $\beta_{\pm,K}=0$ means complete
reversal, whereas $\beta_{\pm,K}=1$ means that the initial intervention
remains in reserves.

To aggregate the two paths, first compute their average signed long-horizon
responses,

$$
\bar r_+
\equiv\widehat\beta_{+,K}\,\overline b_+,
\qquad
\bar r_-
\equiv-\widehat\beta_{-,K}\,\overline b_-,
$$

where $\overline b_+$ and $\overline b_-$ are the mean event sizes. If the
shares of the two event types are $p_+$ and $p_-$, their estimated average
contribution to reserve drift is

$$
\bar r
\equiv
p_+\bar r_+ + p_-\bar r_-.
$$

The statistic has a direct interpretation. If both long-horizon coefficients
are positive but $\bar r=0$, interventions persist individually while
purchases and sales offset on average. If $\bar r>0$, persistent purchases
dominate and contribute to upward reserve drift; if $\bar r<0$, persistent
sales dominate. Under symmetric misclassification, the benchmark is
$\bar r=0$. The added information relative to the flow-reversal test is
therefore whether non-reverting interventions are directionally balanced and
whether their cumulative stock effect is economically important.

For implementation, estimate the horizon regressions in rolling subsamples
to distinguish a persistent imbalance from a short episode, and obtain
standard errors for $\bar r$ with a block bootstrap over event dates. To
limit contamination from overlapping interventions, either retain only
windows with no later flagged event before $K$, or use a stacked design that
censors each window at the next event. Choosing $K$ balances allowing noise
positions to decay against retaining enough non-overlapping observations.

The spot response gives the drift statistic its economic interpretation. A
positive $\bar r$ driven by premium/purchase events followed by persistent
appreciation matches the model's pattern of repeated fundamental
appreciations treated as financial inflows. Failure to find this directional
pattern rejects the joint prediction of one-sided misclassification and no
subsequent correction, although symmetric mistakes may still cancel. Finding
the pattern is constructive evidence for the mechanism, but not unique
attribution: optimal saving after fundamental shocks can also generate
reserve accumulation. Establishing that the accumulation reflects mistakes
rather than fundamentals requires independently identified shocks or
quantity data on the underlying deposit and capital-flow decomposition.


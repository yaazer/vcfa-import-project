# Disclaimer, Assumption of Risk and Limitation of Liability

**vcfa-import** (the "Software")

> **Read this before you install, run, copy or rely on the Software.**
> By doing any of those things you accept every term below. If you do not
> accept them, or you are not authorised to accept them for your organisation,
> do not use the Software.

---

## The short version

These summaries are not a substitute for the full terms below. Where they
differ, the full terms win.

- **This is experimental, unsupported software.** Much of it was written with
  the help of AI code-generation tools. It has had no formal quality assurance,
  no independent code review and no security audit.
- **It changes production infrastructure.** It imports virtual machines,
  changes who owns them, and can make changes that **cannot be undone**.
- **It is not a VMware or Broadcom product, and not a VMware Fling.** No vendor
  supports it.
- **There is no warranty of any kind.** It may not work, and it may do the wrong
  thing.
- **You use it entirely at your own risk.** If anything goes wrong, including
  outages, data loss, or VMs that are lost, broken or in the wrong place, the
  authors and contributors are not responsible.
- **You are responsible for testing it, backing up and protecting your
  environment, and deciding whether it is fit for your purpose.**

---

## 1. Definitions

- **"Authors"** means the people who wrote, contributed to, maintain or
  distribute the Software, and their employers, affiliates, agents and
  successors, in each case only to the extent that they are connected with the
  Software.
- **"You"** means the person using the Software and, if that person uses it for
  an organisation, that organisation as well.
- **"Target Systems"** means every system the Software reads from, writes to or
  otherwise affects. This includes VMware vCenter Server, VMware Cloud
  Foundation, VCF Automation, Supervisor clusters and namespaces, the VCF
  Mobility Operator, Kubernetes API servers, NSX networks and subnets, ESXi
  hosts, datastores, virtual machines, and the workloads and data inside them.
- **"Loss"** means any loss, damage, cost, expense, liability, claim, penalty or
  harm of any kind.

## 2. What the Software is

2.1 **Experimental.** The Software is an early-stage, experimental tool provided
as a technical preview. It is not a finished or production-grade product.

2.2 **AI-assisted.** A substantial part of the Software and its documentation
was produced with AI code-generation tools. It may contain errors, omissions,
security weaknesses, incorrect assumptions about third-party products, and
behaviour that the Authors did not intend or foresee.

2.3 **Not independently verified.** The Software has not been certified,
audited, penetration-tested or validated against any standard. Its automated
tests mostly run against simulated systems. Passing tests, rehearsals,
dry-runs, prechecks or simulations does not show that the Software will behave
correctly against real Target Systems.

2.4 **Built on unstable interfaces.** The Software depends on third-party
interfaces that may change or behave differently from how they are documented,
including pre-release ("alpha") interfaces of the VCF Mobility Operator. A
change to any of them may make the Software fail or act unpredictably without
warning.

## 3. Not a VMware or Broadcom product

3.1 The Software is **not** made, endorsed, sponsored, certified, supported or
distributed by VMware, Broadcom Inc. or any of their affiliates. It is **not**
a VMware Fling and is not offered under the VMware Fling terms.

3.2 VMware, vSphere, vCenter, VMware Cloud Foundation, VCF, NSX and related
names and logos are trademarks of Broadcom Inc. or its subsidiaries. They are
used only to describe what the Software works with, and their use implies no
affiliation or endorsement.

3.3 **Vendor support.** Using the Software with vendor products may fall outside
those vendors' supported procedures. That may affect your support entitlements,
warranties or service-level commitments with those vendors. It is your
responsibility to confirm this with each vendor before use.

## 4. No support or maintenance

The Authors have no obligation to provide support, maintenance, updates,
upgrades, bug fixes, security patches, documentation, training or answers to
questions. They may change, suspend or stop the Software at any time without
notice. Any help they give is voluntary, and it does not create an obligation
to give more help or change these terms.

## 5. No warranty

5.1 TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, THE SOFTWARE AND ITS
DOCUMENTATION ARE PROVIDED **"AS IS" AND "AS AVAILABLE", WITH ALL FAULTS AND
WITHOUT WARRANTY OF ANY KIND**, WHETHER EXPRESS, IMPLIED, STATUTORY OR
OTHERWISE.

5.2 THE AUTHORS SPECIFICALLY DISCLAIM ALL WARRANTIES AND CONDITIONS OF
MERCHANTABILITY, SATISFACTORY QUALITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE,
NON-INFRINGEMENT, ACCURACY, COMPLETENESS, RELIABILITY, AVAILABILITY,
COMPATIBILITY, SECURITY, DATA INTEGRITY AND QUIET ENJOYMENT, AND ANY
WARRANTIES ARISING FROM A COURSE OF DEALING, USAGE OR TRADE PRACTICE.

5.3 IN PARTICULAR, THE AUTHORS DO NOT WARRANT THAT THE SOFTWARE WILL:
(a) work as described; (b) be free of errors, defects, bugs or vulnerabilities;
(c) import, place, network, preserve, commit, roll back, verify or clean up
any virtual machine correctly; (d) preserve data, configuration, identity,
network addresses or availability; (e) report states, results or errors
accurately; or (f) work with any version of any Target System.

5.4 Nothing the Authors say or write (in the documentation, guides, comments,
examples, issue trackers, conversations or elsewhere) creates any warranty
or changes these terms.

## 6. Risks you accept

You acknowledge that you understand the following risks and accept them in
full:

6.1 **Irreversible changes.** Committing an import, whether automatically under
`commitAction: Auto` or by releasing a commit gate, is designed to be
irreversible. After a commit, the Software cannot return a virtual machine to
its previous state.

6.2 **Batch-wide operations.** Commit, rollback, abandon and clean-up actions
apply to whole batches. An action you aim at one virtual machine may affect
others in the same batch. Previews, prompts and confirmation messages may not
list every affected virtual machine.

6.3 **Ownership changes.** The Software changes which system controls a virtual
machine. A failed, interrupted or partial operation may leave a machine in an
inconsistent, unmanaged, duplicated or unrecoverable state, under the control
of neither vCenter nor the Supervisor, or of both.

6.4 **Outages and data loss.** Using the Software may cause downtime, power-state
changes, loss or corruption of data or configuration, network disruption, IP
address conflicts, security exposure, performance problems, or exhaustion of
storage, compute or other resources.

6.5 **Wrong placement.** Mapping files, tags, folders, settings, defaults or
logic errors may place virtual machines in the wrong namespace, subnet, network
or security zone, or apply the wrong policy to them.

6.6 **Automation at scale.** The Software is built to act on large numbers of
virtual machines at once, and in parallel. A single mistake, setting or defect
may affect a large part of your environment before anyone notices.

6.7 **Credentials and access.** The Software uses credentials and access tokens
to systems such as vCenter and Kubernetes, and stores inventory information
locally (including names, addresses, folder structures and tags). How you
protect, limit and rotate that access and that data is entirely your
responsibility.

6.8 **Scheduled and unattended runs.** Scheduled, scripted or unattended
operations may run when no one is watching, and may keep running after
conditions change.

## 7. Your responsibilities

As a condition of using the Software, you are solely responsible for:

(a) deciding, on your own independent judgement, whether the Software is
suitable for your purpose;

(b) testing the Software thoroughly in a non-production environment that
represents your production environment before using it on any production
system;

(c) taking, checking and being able to restore backups and recovery points of
every affected virtual machine and system, and having a manual recovery plan
that does not depend on the Software;

(d) reviewing every plan, manifest, batch and command before applying it, and
confirming what each commit, rollback, abandon or clean-up action will affect;

(e) using least-privilege accounts and protecting every credential, token,
configuration and data file the Software uses or creates;

(f) following your organisation's change-management, approval, security and
operational policies, and those of your customers;

(g) making sure only suitably qualified and trained people operate the
Software;

(h) following the terms, documentation and supported procedures of every
vendor whose products the Software touches;

(i) complying with all laws and regulations that apply to you, including those
on data protection, privacy and export control; and

(j) monitoring every operation until it completes and stepping in if something
goes wrong.

## 8. Limitation of liability

8.1 TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, IN NO EVENT WILL THE
AUTHORS BE LIABLE TO YOU OR ANY THIRD PARTY FOR ANY LOSS ARISING OUT OF OR
CONNECTED WITH THE SOFTWARE OR ITS USE, MISUSE OR INABILITY TO BE USED.
THIS INCLUDES, WITHOUT LIMITATION:

(a) direct, indirect, incidental, special, consequential, exemplary or
punitive damages;

(b) loss of data, workloads, virtual machines, configuration, profits,
revenue, business, contracts, goodwill, reputation or anticipated savings;

(c) business interruption, downtime or service-level failures;

(d) the cost of recovery, restoration, remediation, replacement services,
substitute systems or incident response;

(e) security incidents, unauthorised access or data breaches; and

(f) claims, penalties or service credits owed to your customers, users,
employer or any other third party.

8.2 This limitation applies whatever the legal theory, including contract, tort
(including negligence), strict liability, breach of statutory duty,
misrepresentation or any other theory. It applies even if the Authors were told
about, knew of, or could have foreseen the possibility of such Loss, and even if
any remedy fails of its essential purpose.

8.3 TO THE EXTENT THAT ANY LIABILITY OF THE AUTHORS CANNOT BE EXCLUDED UNDER
APPLICABLE LAW, THEIR TOTAL AGGREGATE LIABILITY FOR ALL CLAIMS COMBINED IS
LIMITED TO THE AMOUNT YOU PAID FOR THE SOFTWARE, IF ANY.

## 9. Release and indemnity

9.1 **Release.** To the maximum extent permitted by applicable law, you release
and forever discharge the Authors from all claims, demands and Loss, known or
unknown, arising out of or connected with the Software or its use.

9.2 **Indemnity.** You agree to defend, indemnify and hold harmless the Authors
against all Loss, including reasonable legal fees, arising out of or connected
with: (a) your use or misuse of the Software; (b) your breach of these terms;
(c) any change you make to the Software, or any version of it you distribute;
or (d) any claim brought by your customers, users, employer or any other third
party in connection with your use of the Software.

## 10. Documentation, examples and output

The documentation, guides, examples, sample configurations, reports, estimates,
readiness grades, verification results and other output of the Software are
provided for information only. They are not professional, engineering, legal or
operational advice, and may be incomplete, out of date or wrong. Check them
independently before relying on them.

## 11. Third-party software and services

11.1 The Software works with, calls or depends on third-party software and
services, including `kubectl`, Python, VMware vCenter, VMware Cloud Foundation
and the VCF Mobility Operator. Those are governed only by their own licences and
terms. The Authors make no promises about them and are not responsible for their
behaviour, availability, or changes to them.

11.2 Some distributions of the Software, notably the Windows executable, contain
third-party components. Those are listed in `THIRD-PARTY-NOTICES.md` with their
licences, and your use of each component is also governed by its licence.

11.3 **Microsoft Distributable Code.** If you use the Windows executable, it
contains Microsoft Distributable Code, including the Visual C++ runtime and code
linked into the Python runtime. You agree not to: (a) alter any copyright,
trademark or patent notice in it; (b) use Microsoft's trademarks in a way that
suggests the Software comes from or is endorsed by Microsoft; (c) run it on any
platform other than Microsoft operating systems, run-time technologies or
application platforms; or (d) include it in any malicious, deceptive or unlawful
program. If you redistribute the Software, you must require your recipients to
agree to these same restrictions.

## 12. Changes you make

If you modify, combine, repackage or redistribute the Software, the Authors have
no responsibility for the result. You must keep this disclaimer, without
alteration, in every copy or substantial portion you distribute, and you are
responsible for any additional commitments you make to anyone else.

## 13. Acting for an organisation

If you use the Software for an employer, customer or other organisation, you
confirm that you are authorised to accept these terms on its behalf, and that
using the Software is allowed under its policies and under any contract that
applies to the Target Systems. If you are not authorised, you must not use the
Software.

## 14. What is not excluded

Nothing in these terms excludes or limits any liability that cannot be excluded
or limited under applicable law (for example, in some places, liability for
fraud, or for death or personal injury caused by negligence). If such a law
applies, the Authors' liability is limited to the smallest extent that law
permits, and every other term stays in full force.

## 15. General

15.1 **Severability.** If any part of these terms is found invalid or
unenforceable, it will be enforced to the maximum extent permitted, and the rest
of the terms will stay in full force.

15.2 **Survival.** These terms continue to apply after you stop using the
Software.

15.3 **No waiver.** If the Authors do not enforce a term, that does not waive
their right to enforce it later.

15.4 **Relationship to the licence.** The Software's source code is licensed
under the Apache License, Version 2.0 (`LICENSE`). That licence also disclaims
warranties and limits liability (its sections 7 and 8). These terms add to the
licence and do not take away any right it grants you to use, copy, modify or
distribute the source code. However, running the Software requires accepting
these terms, and they govern how you use it. If anything in these terms
conflicts with the licence, the licence decides what you may do with the code,
and these terms decide who bears the risk of running it, to the extent permitted
by law.

15.5 **How you accept.** The Software asks you to accept these terms the first
time you run it, from the command line or in the web console, and again whenever
they change. It records who accepted, when, and which version of the terms. Each
person who uses the Software must accept for themselves. Whoever installs the
Software for scheduled or unattended use must accept on behalf of that account.

15.6 **Governing law.** These terms are governed by the laws of
**[JURISDICTION — to be completed]**, without regard to its conflict-of-law rules.
The courts of **[VENUE — to be completed]** have exclusive jurisdiction over any
dispute about them.

15.7 **Changes.** The Authors may update these terms at any time by publishing a
new version with the Software. Using a version of the Software means you accept
the terms that come with that version.

15.8 **Contact.** **[CONTACT — to be completed]**

---

*Disclaimer version 1.0. Applies to vcfa-import 1.1.1 and later.*

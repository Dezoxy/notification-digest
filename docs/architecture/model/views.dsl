// Each view answers one question for one audience. Budgets from the
// architecture-views skill: 7 elements/8 arrows for an audience overview,
// 10/12 for a technical structural view, 7 participants/8 interactions for a
// runtime scenario, 12 boxes/8 arrows for deployment. The register in
// ../README.md records each view's question, omissions and update trigger.
//
// The seven collection sources are the reason this is not one context view:
// including them all blows the overview budget, so "Context" answers the
// delivery question and "Sources" answers the collection question.

systemLandscape "Landscape" "Which systems does the digest sit between?" {
    include *
    exclude githubActions imageRegistry renovate keyVault
    autoLayout lr
}

systemContext notificationDigest "Context" "Who reads the digest, what writes it, and where does it come out?" {
    include owner notificationDigest anthropic telegram smtpRelay
    autoLayout lr
}

systemContext notificationDigest "Sources" "Which accounts and feeds does a run collect from?" {
    include notificationDigest telegram x reddit patreon polymarket hackerNews newsFeeds
    autoLayout tb
}

// Collection sources other than Telegram are the Sources view's question; left
// in, their seven arrows crossed the state and site boxes in every automatic
// layout. SMTP is excluded because it is disabled in production.
container notificationDigest "Containers" "What are the building blocks, and which side of the trust boundary is each on?" {
    include *
    exclude x reddit patreon polymarket hackerNews newsFeeds smtpRelay
    exclude githubActions imageRegistry keyVault
    exclude anthropic entra
    autoLayout tb 300 150
}

container notificationDigest "Security" "What is reachable from the internet, where do secrets and identity come from, and what stays private?" {
    include notificationDigest.runner notificationDigest.state notificationDigest.site notificationDigest.siteDb
    include owner keyVault entra githubActions
    autoLayout tb 300 150
}

dynamic notificationDigest "DigestRun" "What happens during one scheduled digest run?" {
    notificationDigest.runner -> notificationDigest.state "Restores the state and reads the last-seen cursor per source" "SQLite bundle, Blob lease"
    notificationDigest.runner -> telegram "Fetches messages newer than the cursor" "MTProto, user session"
    notificationDigest.runner -> notificationDigest.state "Records the new items" "SQLite bundle, Blob lease"
    notificationDigest.runner -> anthropic "Summarizes the window" "Claude CLI, headless subprocess"
    notificationDigest.runner -> notificationDigest.site "Publishes the rendered digest" "HTTPS/JSON, shared-secret ingest"
    notificationDigest.runner -> telegram "Sends the summary and deep links" "HTTPS, Telegram Bot API"
    notificationDigest.runner -> notificationDigest.state "Marks the digest delivered and checkpoints the state" "SQLite bundle, Blob lease"
    autoLayout lr
}

// How a change reaches the jobs. Deliberately separate from the runtime views:
// none of this happens during a run.
container notificationDigest "Delivery" "How does a code change reach the running jobs?" {
    include notificationDigest.runner
    include owner githubActions imageRegistry renovate keyVault
    autoLayout lr
}

deployment notificationDigest "Production" "ProductionDeployment" "Where does the digest run, and what fails together?" {
    include *
    autoLayout tb
}

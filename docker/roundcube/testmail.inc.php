<?php
// Roundcube for the local test mail server (compose profile "testmail"), included by the image
// after its generated config. GreenMail speaks plain IMAP/SMTP; SMTP AUTH reuses the IMAP login.
$config['product_name'] = 'Finn Testpostfach';
$config['language'] = 'de_DE';
$config['smtp_user'] = '%u';
$config['smtp_pass'] = '%p';
$config['prefer_html'] = true;          // show Finn's HTML part, as Outlook would
$config['create_default_folders'] = true;
$config['draft_autosave'] = 0;

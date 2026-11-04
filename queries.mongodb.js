/* global use, db */
// auto-submit - READ-ONLY queries for the MongoDB for VS Code extension.
// Nothing in this file inserts, updates or deletes anything.
// To change data use the board (http://127.0.0.1:8765) - it has Undo.
//
// Run all:  the ▶ button at the top right.
// Run one:  select its lines -> right click -> "MongoDB: Run Selected Lines From Playground".
// The last query's result opens in the results panel; console.log() output goes to "Playground output".

use('autosubmit');

// 1) Jobs per board stage
console.log('Jobs per stage:', db.jobs.aggregate([
  { $match: { keep: true } },
  { $group: { _id: '$status', jobs: { $sum: 1 } } },
  { $sort: { jobs: -1 } },
]).toArray());

// 2) Why jobs were dropped by the filters
console.log('Dropped because:', db.jobs.aggregate([
  { $match: { keep: false } },
  { $group: { _id: '$reason', jobs: { $sum: 1 } } },
  { $sort: { jobs: -1 } },
]).toArray());

// 3) Last scan (appears after the first 17:00 scan)
console.log('Last scan:', db.meta.findOne({ _id: 'last_scan' }));

// 4) Saved form answers (from apply.py)
console.log('Saved answers:', db.answers.find({}, { _id: 0, label: 1, value: 1 }).toArray());

// 5) Review (score 50-69), best first - candidates to drag into "To apply"
console.log('Review:', db.jobs.find(
  { keep: true, status: 'review' },
  { _id: 0, score: 1, title: 1, company: 1, match_reason: 1 },
).sort({ score: -1 }).toArray());

// 6) One job with its full description - put the job id from the URL
// db.jobs.findOne({ _id: '4486050709' }, { title: 1, company: 1, description: 1 });

// 7) Search by company or title (case-insensitive)
// db.jobs.find({ keep: true, company: /drivenets/i }, { _id: 0, title: 1, status: 1, score: 1 });

// 8) To apply, best first (shown in the results panel)
db.jobs.find(
  { keep: true, status: 'queued' },
  { _id: 0, score: 1, title: 1, company: 1, easy_apply: 1, url: 1 },
).sort({ score: -1 });

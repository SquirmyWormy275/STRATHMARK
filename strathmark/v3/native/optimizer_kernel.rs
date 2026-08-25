//! Exact candidate-evaluation kernel for the V3 fairness-frontier optimizer.
//!
//! This library is an acceleration of the frozen Python integer authority. It
//! computes only winner credits and finish spread; legality, search, Pareto
//! dominance, CHIM selection, receipts, and replay remain in Python.

use std::sync::{mpsc, Arc, Mutex, OnceLock};
use std::thread::{self, JoinHandle};

const ABI_VERSION: u32 = 1;
const MAX_ENTRANTS: usize = 12;
const REQUIRED_DRAWS: usize = 4096;
const THRESHOLD_COUNT: usize = 361;
const THRESHOLD_OFFSET: i32 = 180;
const WORKER_COUNT: usize = 8;

struct EvaluationCore {
    samples: Vec<i32>,
    draw_count: usize,
    entrant_count: usize,
    word_count: usize,
    comparison_masks: Vec<u64>,
}

impl EvaluationCore {
    fn mask_offset(&self, left: usize, right: usize, threshold_index: usize) -> usize {
        ((left * self.entrant_count + right) * THRESHOLD_COUNT + threshold_index)
            * self.word_count
    }
}

enum WorkerCommand {
    Evaluate {
        delays_address: usize,
        start: usize,
        end: usize,
        credit_scale: i64,
        spreads_address: usize,
        credits_address: usize,
        completed: mpsc::Sender<()>,
    },
    Shutdown,
}

struct Worker {
    sender: mpsc::Sender<WorkerCommand>,
    handle: Option<JoinHandle<()>>,
}

struct WorkerPool {
    workers: Vec<Worker>,
    evaluation_lock: Mutex<()>,
}

enum ParetoCommand {
    Mark {
        sources_address: usize,
        source_count: usize,
        targets_address: usize,
        start: usize,
        end: usize,
        dominated_address: usize,
        nonstrict_address: usize,
        strict_address: usize,
        completed: mpsc::Sender<()>,
    },
}

struct ParetoWorkerPool {
    senders: Vec<mpsc::Sender<ParetoCommand>>,
    _handles: Vec<JoinHandle<()>>,
    evaluation_lock: Mutex<()>,
}

impl ParetoWorkerPool {
    fn new() -> Result<Self, ()> {
        let mut senders = Vec::with_capacity(WORKER_COUNT);
        let mut handles = Vec::with_capacity(WORKER_COUNT);
        for ordinal in 0..WORKER_COUNT {
            let (sender, receiver) = mpsc::channel();
            let handle = thread::Builder::new()
                .name(format!("strathmark-v3-pareto-{ordinal}"))
                .spawn(move || {
                    while let Ok(ParetoCommand::Mark {
                        sources_address,
                        source_count,
                        targets_address,
                        start,
                        end,
                        dominated_address,
                        nonstrict_address,
                        strict_address,
                        completed,
                    }) = receiver.recv()
                    {
                        unsafe {
                            let sources = std::slice::from_raw_parts(
                                sources_address as *const i64,
                                source_count * 4,
                            );
                            let targets = std::slice::from_raw_parts(
                                (targets_address as *const i64).add(start * 4),
                                (end - start) * 4,
                            );
                            let dominated = std::slice::from_raw_parts_mut(
                                (dominated_address as *mut u8).add(start),
                                end - start,
                            );
                            let nonstrict = std::slice::from_raw_parts(
                                nonstrict_address as *const i64,
                                4,
                            );
                            let strict = std::slice::from_raw_parts(
                                strict_address as *const i64,
                                4,
                            );
                            mark_dominated_rows(
                                sources,
                                targets,
                                dominated,
                                nonstrict,
                                strict,
                            );
                        }
                        let _ = completed.send(());
                    }
                })
                .map_err(|_| ())?;
            senders.push(sender);
            handles.push(handle);
        }
        Ok(Self {
            senders,
            _handles: handles,
            evaluation_lock: Mutex::new(()),
        })
    }

    unsafe fn mark(
        &self,
        sources_ptr: *const i64,
        source_count: usize,
        targets_ptr: *const i64,
        target_count: usize,
        dominated_ptr: *mut u8,
        nonstrict_ptr: *const i64,
        strict_ptr: *const i64,
    ) -> i32 {
        let Ok(_guard) = self.evaluation_lock.lock() else {
            return 3;
        };
        let workers = usize::min(self.senders.len(), target_count);
        let (completed_sender, completed_receiver) = mpsc::channel();
        let mut dispatched = 0_usize;
        for (ordinal, sender) in self.senders.iter().take(workers).enumerate() {
            let start = ordinal * target_count / workers;
            let end = (ordinal + 1) * target_count / workers;
            let command = ParetoCommand::Mark {
                sources_address: sources_ptr as usize,
                source_count,
                targets_address: targets_ptr as usize,
                start,
                end,
                dominated_address: dominated_ptr as usize,
                nonstrict_address: nonstrict_ptr as usize,
                strict_address: strict_ptr as usize,
                completed: completed_sender.clone(),
            };
            if sender.send(command).is_err() {
                break;
            }
            dispatched += 1;
        }
        drop(completed_sender);
        for _ in 0..dispatched {
            if completed_receiver.recv().is_err() {
                return 3;
            }
        }
        if dispatched == workers { 0 } else { 3 }
    }
}

static PARETO_POOL: OnceLock<Option<ParetoWorkerPool>> = OnceLock::new();

impl WorkerPool {
    fn new(core: &Arc<EvaluationCore>) -> Result<Self, ()> {
        let mut workers: Vec<Worker> = Vec::with_capacity(WORKER_COUNT);
        for ordinal in 0..WORKER_COUNT {
            let (sender, receiver) = mpsc::channel();
            let worker_core = Arc::clone(core);
            let handle = thread::Builder::new()
                .name(format!("strathmark-v3-optimizer-{ordinal}"))
                .spawn(move || {
                    let mut winner_masks =
                        vec![0_u64; worker_core.entrant_count * worker_core.word_count];
                    while let Ok(command) = receiver.recv() {
                        match command {
                            WorkerCommand::Evaluate {
                                delays_address,
                                start,
                                end,
                                credit_scale,
                                spreads_address,
                                credits_address,
                                completed,
                            } => {
                                unsafe {
                                    evaluate_range(
                                        &worker_core,
                                        delays_address as *const i32,
                                        start,
                                        end,
                                        credit_scale,
                                        spreads_address as *mut i64,
                                        credits_address as *mut i64,
                                        &mut winner_masks,
                                    );
                                }
                                let _ = completed.send(());
                            }
                            WorkerCommand::Shutdown => break,
                        }
                    }
                })
                .map_err(|_| ())?;
            workers.push(Worker {
                sender,
                handle: Some(handle),
            });
        }
        Ok(Self {
            workers,
            evaluation_lock: Mutex::new(()),
        })
    }

    unsafe fn evaluate(
        &self,
        delays_ptr: *const i32,
        candidate_count: usize,
        credit_scale: i64,
        spreads_ptr: *mut i64,
        credits_ptr: *mut i64,
    ) -> i32 {
        let Ok(_guard) = self.evaluation_lock.lock() else {
            return 3;
        };
        let workers = usize::min(self.workers.len(), candidate_count);
        let (completed_sender, completed_receiver) = mpsc::channel();
        let mut dispatched = 0_usize;
        for (ordinal, worker) in self.workers.iter().take(workers).enumerate() {
            let start = ordinal * candidate_count / workers;
            let end = (ordinal + 1) * candidate_count / workers;
            let command = WorkerCommand::Evaluate {
                delays_address: delays_ptr as usize,
                start,
                end,
                credit_scale,
                spreads_address: spreads_ptr as usize,
                credits_address: credits_ptr as usize,
                completed: completed_sender.clone(),
            };
            if worker.sender.send(command).is_err() {
                break;
            }
            dispatched += 1;
        }
        drop(completed_sender);
        for _ in 0..dispatched {
            if completed_receiver.recv().is_err() {
                return 3;
            }
        }
        if dispatched == workers {
            0
        } else {
            // All successfully dispatched ranges completed before failure is
            // reported, so no worker can retain a caller-owned output pointer.
            3
        }
    }
}

impl Drop for WorkerPool {
    fn drop(&mut self) {
        for worker in &self.workers {
            let _ = worker.sender.send(WorkerCommand::Shutdown);
        }
        for worker in &mut self.workers {
            if let Some(handle) = worker.handle.take() {
                let _ = handle.join();
            }
        }
    }
}

struct EvaluationContext {
    core: Arc<EvaluationCore>,
    worker_pool: WorkerPool,
}

#[unsafe(no_mangle)]
pub extern "C" fn strathmark_v3_optimizer_kernel_abi_version() -> u32 {
    ABI_VERSION
}

const SHA256_INITIAL: [u32; 8] = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];
const SHA256_ROUNDS: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

fn sha256_first_u64_single_block(message: &[u8]) -> Option<u64> {
    if message.len() > 55 {
        return None;
    }
    let mut block = [0_u8; 64];
    block[..message.len()].copy_from_slice(message);
    block[message.len()] = 0x80;
    block[56..].copy_from_slice(&((message.len() as u64) * 8).to_be_bytes());
    let mut schedule = [0_u32; 64];
    for (index, chunk) in block.chunks_exact(4).enumerate() {
        schedule[index] = u32::from_be_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
    }
    for index in 16..64 {
        let left = schedule[index - 15];
        let right = schedule[index - 2];
        let sigma0 = left.rotate_right(7) ^ left.rotate_right(18) ^ (left >> 3);
        let sigma1 = right.rotate_right(17) ^ right.rotate_right(19) ^ (right >> 10);
        schedule[index] = schedule[index - 16]
            .wrapping_add(sigma0)
            .wrapping_add(schedule[index - 7])
            .wrapping_add(sigma1);
    }
    let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = SHA256_INITIAL;
    for index in 0..64 {
        let upper = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
        let choice = (e & f) ^ ((!e) & g);
        let first = h
            .wrapping_add(upper)
            .wrapping_add(choice)
            .wrapping_add(SHA256_ROUNDS[index])
            .wrapping_add(schedule[index]);
        let lower = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
        let majority = (a & b) ^ (a & c) ^ (b & c);
        let second = lower.wrapping_add(majority);
        h = g;
        g = f;
        f = e;
        e = d.wrapping_add(first);
        d = c;
        c = b;
        b = a;
        a = first.wrapping_add(second);
    }
    let first = SHA256_INITIAL[0].wrapping_add(a);
    let second = SHA256_INITIAL[1].wrapping_add(b);
    Some((u64::from(first) << 32) | u64::from(second))
}

fn append_decimal(buffer: &mut [u8; 64], length: &mut usize, mut value: u64) -> Option<()> {
    let mut digits = [0_u8; 20];
    let mut count = 0_usize;
    loop {
        digits[count] = b'0' + (value % 10) as u8;
        count += 1;
        value /= 10;
        if value == 0 {
            break;
        }
    }
    if *length + count > buffer.len() {
        return None;
    }
    for index in (0..count).rev() {
        buffer[*length] = digits[index];
        *length += 1;
    }
    Some(())
}

fn decimal_uniform(hash_prefix: u64) -> Option<(u128, u8)> {
    const DENOMINATOR: u128 = (u64::MAX as u128) + 2;
    let numerator = u128::from(hash_prefix) + 1;
    let digits = numerator.ilog10() as i32 + 1;
    let mut exponent = if digits >= 20 { -1 } else { digits - 20 };
    if numerator.checked_mul(10_u128.pow((-exponent) as u32))? < DENOMINATOR {
        exponent -= 1;
    }
    let scale = (27 - exponent) as u8;
    let mut quotient = 0_u128;
    let mut remainder = numerator;
    let mut remaining = u32::from(scale);
    while remaining != 0 {
        let width = remaining.min(19);
        let factor = 10_u128.pow(width);
        let product = remainder.checked_mul(factor)?;
        quotient = quotient.checked_mul(factor)?.checked_add(product / DENOMINATOR)?;
        remainder = product % DENOMINATOR;
        remaining -= width;
    }
    let doubled = remainder.checked_mul(2)?;
    if doubled > DENOMINATOR || (doubled == DENOMINATOR && quotient % 2 == 1) {
        quotient = quotient.checked_add(1)?;
    }
    Some((quotient, scale))
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_generate_independent_rank_uniforms(
    seed: u64,
    draw_count: usize,
    stream_count: usize,
    quotient_words_ptr: *mut u64,
    scales_ptr: *mut u8,
) -> i32 {
    if quotient_words_ptr.is_null()
        || scales_ptr.is_null()
        || draw_count != REQUIRED_DRAWS
        || !(1..=MAX_ENTRANTS).contains(&stream_count)
    {
        return 1;
    }
    let cell_count = draw_count * stream_count;
    let quotient_words = unsafe {
        std::slice::from_raw_parts_mut(quotient_words_ptr, cell_count * 2)
    };
    let scales = unsafe { std::slice::from_raw_parts_mut(scales_ptr, cell_count) };
    for draw in 0..draw_count {
        for stream in 0..stream_count {
            let mut message = [0_u8; 64];
            let prefix = b"field-crn-v1:";
            message[..prefix.len()].copy_from_slice(prefix);
            let mut length = prefix.len();
            if append_decimal(&mut message, &mut length, seed).is_none() {
                return 2;
            }
            message[length] = b':';
            length += 1;
            if append_decimal(&mut message, &mut length, draw as u64).is_none() {
                return 2;
            }
            let stream_prefix = b":crn:";
            message[length..length + stream_prefix.len()].copy_from_slice(stream_prefix);
            length += stream_prefix.len();
            if append_decimal(&mut message, &mut length, stream as u64).is_none() {
                return 2;
            }
            let Some(hash_prefix) = sha256_first_u64_single_block(&message[..length]) else {
                return 2;
            };
            let Some((quotient, scale)) = decimal_uniform(hash_prefix) else {
                return 2;
            };
            let cell = draw * stream_count + stream;
            quotient_words[cell * 2] = quotient as u64;
            quotient_words[cell * 2 + 1] = (quotient >> 64) as u64;
            scales[cell] = scale;
        }
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_sample_three_quantiles(
    probability_words_ptr: *const u64,
    draw_count: usize,
    times_ptr: *const i32,
    distribution_count: usize,
    output_ptr: *mut i32,
) -> i32 {
    if probability_words_ptr.is_null()
        || times_ptr.is_null()
        || output_ptr.is_null()
        || draw_count != REQUIRED_DRAWS
        || !(1..=3).contains(&distribution_count)
    {
        return 1;
    }
    let probability_words =
        unsafe { std::slice::from_raw_parts(probability_words_ptr, draw_count * 2) };
    let times = unsafe { std::slice::from_raw_parts(times_ptr, distribution_count * 3) };
    let output =
        unsafe { std::slice::from_raw_parts_mut(output_ptr, distribution_count * draw_count) };
    const P10: u128 = 1_000_000_000_000_000_000_000_000_000;
    const P50: u128 = 5_000_000_000_000_000_000_000_000_000;
    const P90: u128 = 9_000_000_000_000_000_000_000_000_000;
    const INTERVAL: u128 = 4_000_000_000_000_000_000_000_000_000;
    for distribution in 0..distribution_count {
        let row = &times[distribution * 3..(distribution + 1) * 3];
        if row[0] <= 0
            || row[0] > row[1]
            || row[1] > row[2]
            || row[2] > 2_000_000_000
        {
            return 2;
        }
        for draw in 0..draw_count {
            let probability = u128::from(probability_words[draw * 2])
                | (u128::from(probability_words[draw * 2 + 1]) << 64);
            let value = if probability <= P10 {
                row[0]
            } else if probability > P90 {
                row[2]
            } else {
                let (left_probability, left_time, right_time) = if probability <= P50 {
                    (P10, row[0], row[1])
                } else {
                    (P50, row[1], row[2])
                };
                let exact = u128::from(left_time as u32) * INTERVAL
                    + (probability - left_probability)
                        * u128::from((right_time - left_time) as u32);
                let mut rounded = exact / INTERVAL;
                let remainder = exact % INTERVAL;
                let doubled = remainder * 2;
                if doubled > INTERVAL || (doubled == INTERVAL && rounded % 2 == 1) {
                    rounded += 1;
                }
                if rounded > 2_000_000_000 {
                    return 2;
                }
                rounded as i32
            };
            output[distribution * draw_count + draw] = value;
        }
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_sample_linear_pool_three_quantiles(
    probability_words_ptr: *const u64,
    draw_count: usize,
    weight_words_ptr: *const u64,
    component_count: usize,
    probability_factor: u64,
    times_ptr: *const i32,
    output_ptr: *mut i32,
) -> i32 {
    if probability_words_ptr.is_null()
        || weight_words_ptr.is_null()
        || times_ptr.is_null()
        || output_ptr.is_null()
        || draw_count != REQUIRED_DRAWS
        || !(2..=3).contains(&component_count)
        || probability_factor == 0
    {
        return 1;
    }
    let probability_words =
        unsafe { std::slice::from_raw_parts(probability_words_ptr, draw_count * 2) };
    let weight_words =
        unsafe { std::slice::from_raw_parts(weight_words_ptr, component_count * 2) };
    let times = unsafe { std::slice::from_raw_parts(times_ptr, component_count * 3) };
    let output = unsafe { std::slice::from_raw_parts_mut(output_ptr, draw_count) };
    let mut weights = [0_u128; 3];
    let mut scale = 0_u128;
    for component in 0..component_count {
        let weight = u128::from(weight_words[component * 2])
            | (u128::from(weight_words[component * 2 + 1]) << 64);
        if weight == 0 {
            return 2;
        }
        let Some(next_scale) = scale.checked_add(weight) else {
            return 2;
        };
        scale = next_scale;
        weights[component] = weight;
        let row = &times[component * 3..(component + 1) * 3];
        if row[0] <= 0
            || row[0] > row[1]
            || row[1] > row[2]
            || row[2] > 2_000_000_000
        {
            return 2;
        }
    }
    for draw in 0..draw_count {
        let base_probability = u128::from(probability_words[draw * 2])
            | (u128::from(probability_words[draw * 2 + 1]) << 64);
        let Some(probability) = base_probability.checked_mul(u128::from(probability_factor))
        else {
            return 2;
        };
        if probability >= scale {
            return 2;
        }
        let mut component = component_count - 1;
        let mut left_edge = 0_u128;
        let mut right_edge = 0_u128;
        for (index, weight) in weights[..component_count].iter().copied().enumerate() {
            let Some(next_edge) = right_edge.checked_add(weight) else {
                return 2;
            };
            if probability < next_edge {
                component = index;
                left_edge = right_edge;
                break;
            }
            right_edge = next_edge;
        }
        let local_numerator = probability - left_edge;
        let weight = weights[component];
        let Some(ten_probability) = local_numerator.checked_mul(10) else {
            return 2;
        };
        let Some(nine_weight) = weight.checked_mul(9) else {
            return 2;
        };
        let Some(five_weight) = weight.checked_mul(5) else {
            return 2;
        };
        let row = &times[component * 3..(component + 1) * 3];
        let value = if ten_probability <= weight {
            row[0]
        } else if ten_probability > nine_weight {
            row[2]
        } else {
            let (left_grid, left_time, right_time) = if ten_probability <= five_weight {
                (1_u128, row[0], row[1])
            } else {
                (5_u128, row[1], row[2])
            };
            let Some(left_boundary) = left_grid.checked_mul(weight) else {
                return 2;
            };
            let ratio_numerator = ten_probability - left_boundary;
            let Some(ratio_denominator) = weight.checked_mul(4) else {
                return 2;
            };
            let delta = (right_time - left_time) as u32;
            let (mut offset, remainder) =
                mul_div_rem_u128_small(ratio_numerator, delta, ratio_denominator);
            let complement = ratio_denominator - remainder;
            if remainder > complement || (remainder == complement && offset % 2 == 1) {
                offset += 1;
            }
            let Some(rounded) = u128::from(left_time as u32).checked_add(offset) else {
                return 2;
            };
            if rounded > 2_000_000_000 {
                return 2;
            }
            rounded as i32
        };
        output[draw] = value;
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_sample_linear_pool_quantiles(
    probability_words_ptr: *const u64,
    draw_count: usize,
    weight_words_ptr: *const u64,
    component_count: usize,
    probability_factor: u64,
    grid_words_ptr: *const u64,
    quantile_count: usize,
    times_ptr: *const i32,
    output_ptr: *mut i32,
) -> i32 {
    const GRID_SCALE: u128 = 10_000_000_000_000_000_000_000_000_000;
    if probability_words_ptr.is_null()
        || weight_words_ptr.is_null()
        || grid_words_ptr.is_null()
        || times_ptr.is_null()
        || output_ptr.is_null()
        || draw_count != REQUIRED_DRAWS
        || !(2..=3).contains(&component_count)
        || !(3..=16).contains(&quantile_count)
        || probability_factor == 0
    {
        return 1;
    }
    let probability_words =
        unsafe { std::slice::from_raw_parts(probability_words_ptr, draw_count * 2) };
    let weight_words =
        unsafe { std::slice::from_raw_parts(weight_words_ptr, component_count * 2) };
    let grid_words = unsafe { std::slice::from_raw_parts(grid_words_ptr, quantile_count * 2) };
    let times = unsafe {
        std::slice::from_raw_parts(times_ptr, component_count * quantile_count)
    };
    let output = unsafe { std::slice::from_raw_parts_mut(output_ptr, draw_count) };
    let mut weights = [0_u128; 3];
    let mut scale = 0_u128;
    for component in 0..component_count {
        let weight = u128::from(weight_words[component * 2])
            | (u128::from(weight_words[component * 2 + 1]) << 64);
        if weight == 0 {
            return 2;
        }
        let Some(next_scale) = scale.checked_add(weight) else {
            return 2;
        };
        scale = next_scale;
        weights[component] = weight;
        let row = &times[component * quantile_count..(component + 1) * quantile_count];
        if row[0] <= 0
            || row[row.len() - 1] > 2_000_000_000
            || row.windows(2).any(|pair| pair[0] > pair[1])
        {
            return 2;
        }
    }
    let mut grid = [0_u128; 16];
    for index in 0..quantile_count {
        let value = u128::from(grid_words[index * 2])
            | (u128::from(grid_words[index * 2 + 1]) << 64);
        if value == 0
            || value >= GRID_SCALE
            || (index > 0 && value <= grid[index - 1])
        {
            return 2;
        }
        grid[index] = value;
    }
    for draw in 0..draw_count {
        let base_probability = u128::from(probability_words[draw * 2])
            | (u128::from(probability_words[draw * 2 + 1]) << 64);
        let Some(probability) = base_probability.checked_mul(u128::from(probability_factor))
        else {
            return 2;
        };
        if probability >= scale {
            return 2;
        }
        let mut component = component_count - 1;
        let mut left_edge = 0_u128;
        let mut right_edge = 0_u128;
        for (index, weight) in weights[..component_count].iter().copied().enumerate() {
            let Some(next_edge) = right_edge.checked_add(weight) else {
                return 2;
            };
            if probability < next_edge {
                component = index;
                left_edge = right_edge;
                break;
            }
            right_edge = next_edge;
        }
        let local_numerator = probability - left_edge;
        let weight = weights[component];
        let (scaled_probability, scaled_remainder) =
            mul_div_rem_u128(local_numerator, GRID_SCALE, weight);
        let mut index = 0_usize;
        while index < quantile_count
            && (grid[index] < scaled_probability
                || (grid[index] == scaled_probability && scaled_remainder != 0))
        {
            index += 1;
        }
        let row = &times[component * quantile_count..(component + 1) * quantile_count];
        let value = if index == 0 {
            row[0]
        } else if index == quantile_count {
            row[quantile_count - 1]
        } else {
            let left_grid = grid[index - 1];
            let interval = grid[index] - left_grid;
            let delta = (row[index] - row[index - 1]) as u32;
            let Some(base) = (scaled_probability - left_grid).checked_mul(u128::from(delta))
            else {
                return 2;
            };
            let (fractional, fractional_remainder) =
                mul_div_rem_u128_small(scaled_remainder, delta, weight);
            let Some(total) = base.checked_add(fractional) else {
                return 2;
            };
            let mut offset = total / interval;
            let remainder = total % interval;
            let doubled = remainder * 2;
            let above_half = if doubled == interval {
                fractional_remainder != 0
            } else if doubled + 1 == interval {
                fractional_remainder * 2 > weight
                    || (fractional_remainder * 2 == weight && offset % 2 == 1)
            } else {
                doubled > interval
            };
            let exact_half = doubled == interval && fractional_remainder == 0;
            if above_half || (exact_half && offset % 2 == 1) {
                offset += 1;
            }
            let Some(rounded) = u128::from(row[index - 1] as u32).checked_add(offset) else {
                return 2;
            };
            if rounded > 2_000_000_000 {
                return 2;
            }
            rounded as i32
        };
        output[draw] = value;
    }
    0
}

fn mul_div_rem_u128(value: u128, factor: u128, denominator: u128) -> (u128, u128) {
    debug_assert!(value < denominator);
    let mut quotient = 0_u128;
    let mut remainder = 0_u128;
    for bit in (0..128).rev() {
        quotient *= 2;
        let complement = denominator - remainder;
        if remainder >= complement {
            remainder -= complement;
            quotient += 1;
        } else {
            remainder *= 2;
        }
        if factor & (1_u128 << bit) != 0 {
            let complement = denominator - value;
            if remainder >= complement {
                remainder -= complement;
                quotient += 1;
            } else {
                remainder += value;
            }
        }
    }
    (quotient, remainder)
}

fn mul_div_rem_u128_small(
    value: u128,
    factor: u32,
    denominator: u128,
) -> (u128, u128) {
    debug_assert!(value < denominator);
    let mut quotient = 0_u128;
    let mut remainder = 0_u128;
    for bit in (0..32).rev() {
        quotient *= 2;
        let complement = denominator - remainder;
        if remainder >= complement {
            remainder -= complement;
            quotient += 1;
        } else {
            remainder *= 2;
        }
        if factor & (1_u32 << bit) != 0 {
            let complement = denominator - value;
            if remainder >= complement {
                remainder -= complement;
                quotient += 1;
            } else {
                remainder += value;
            }
        }
    }
    (quotient, remainder)
}

#[derive(Clone, Copy, Eq, PartialEq)]
struct U256([u64; 4]);

impl U256 {
    const ZERO: Self = Self([0; 4]);

    fn compare(self, other: Self) -> std::cmp::Ordering {
        for index in (0..4).rev() {
            match self.0[index].cmp(&other.0[index]) {
                std::cmp::Ordering::Equal => {}
                ordering => return ordering,
            }
        }
        std::cmp::Ordering::Equal
    }

    fn checked_add(self, other: Self) -> Option<Self> {
        let mut result = [0_u64; 4];
        let mut carry = false;
        for (index, slot) in result.iter_mut().enumerate() {
            let (partial, first) = self.0[index].overflowing_add(other.0[index]);
            let (value, second) = partial.overflowing_add(u64::from(carry));
            *slot = value;
            carry = first || second;
        }
        (!carry).then_some(Self(result))
    }

    fn subtract(self, other: Self) -> Self {
        debug_assert!(self.compare(other) != std::cmp::Ordering::Less);
        let mut result = [0_u64; 4];
        let mut borrow = false;
        for (index, slot) in result.iter_mut().enumerate() {
            let (partial, first) = self.0[index].overflowing_sub(other.0[index]);
            let (value, second) = partial.overflowing_sub(u64::from(borrow));
            *slot = value;
            borrow = first || second;
        }
        debug_assert!(!borrow);
        Self(result)
    }

    fn checked_mul_u32(self, factor: u32) -> Option<Self> {
        let mut result = [0_u64; 4];
        let mut carry = 0_u128;
        for (index, slot) in result.iter_mut().enumerate() {
            let product = u128::from(self.0[index]) * u128::from(factor) + carry;
            *slot = product as u64;
            carry = product >> 64;
        }
        (carry == 0).then_some(Self(result))
    }

}

fn div_rem_u256_bounded(
    numerator: U256,
    denominator: U256,
    maximum_quotient: u32,
) -> Option<(u32, U256)> {
    if denominator == U256::ZERO {
        return None;
    }
    // Interpolation offsets can never exceed the adjacent time delta.  Searching
    // that sealed u32 range requires at most 31 four-limb multiplies instead of
    // a general 256-step restoring division, without changing one rounding bit.
    let mut low = 0_u32;
    let mut high = maximum_quotient;
    while low < high {
        let midpoint = low + (high - low + 1) / 2;
        let product = denominator.checked_mul_u32(midpoint)?;
        if product.compare(numerator) != std::cmp::Ordering::Greater {
            low = midpoint;
        } else {
            high = midpoint - 1;
        }
    }
    let product = denominator.checked_mul_u32(low)?;
    Some((low, numerator.subtract(product)))
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_sample_linear_pool_quantiles_wide(
    probability_words_ptr: *const u64,
    draw_count: usize,
    probability_exponent: u32,
    weight_words_ptr: *const u64,
    component_count: usize,
    grid_ptr: *const u32,
    grid_denominator: u32,
    quantile_count: usize,
    times_ptr: *const i32,
    output_ptr: *mut i32,
) -> i32 {
    if probability_words_ptr.is_null()
        || weight_words_ptr.is_null()
        || grid_ptr.is_null()
        || times_ptr.is_null()
        || output_ptr.is_null()
        || draw_count != REQUIRED_DRAWS
        || probability_exponent > 46
        || !(2..=3).contains(&component_count)
        || !(3..=16).contains(&quantile_count)
        || grid_denominator == 0
    {
        return 1;
    }
    let probability_words =
        unsafe { std::slice::from_raw_parts(probability_words_ptr, draw_count * 2) };
    let weight_words = unsafe {
        std::slice::from_raw_parts(weight_words_ptr, component_count * 4)
    };
    let grid = unsafe { std::slice::from_raw_parts(grid_ptr, quantile_count) };
    let times = unsafe {
        std::slice::from_raw_parts(times_ptr, component_count * quantile_count)
    };
    let output = unsafe { std::slice::from_raw_parts_mut(output_ptr, draw_count) };
    if grid[0] == 0
        || grid[quantile_count - 1] >= grid_denominator
        || grid.windows(2).any(|pair| pair[0] >= pair[1])
    {
        return 2;
    }
    let mut weights = [U256::ZERO; 3];
    let mut scale = U256::ZERO;
    for component in 0..component_count {
        let start = component * 4;
        let weight = U256([
            weight_words[start],
            weight_words[start + 1],
            weight_words[start + 2],
            weight_words[start + 3],
        ]);
        if weight == U256::ZERO {
            return 2;
        }
        let Some(next_scale) = scale.checked_add(weight) else {
            return 2;
        };
        scale = next_scale;
        weights[component] = weight;
        let row = &times[component * quantile_count..(component + 1) * quantile_count];
        if row[0] <= 0
            || row[row.len() - 1] > 2_000_000_000
            || row.windows(2).any(|pair| pair[0] > pair[1])
        {
            return 2;
        }
    }
    for draw in 0..draw_count {
        let start = draw * 2;
        let mut probability = U256([
            probability_words[start],
            probability_words[start + 1],
            0,
            0,
        ]);
        for _ in 0..probability_exponent {
            let Some(scaled) = probability.checked_mul_u32(10) else {
                return 2;
            };
            probability = scaled;
        }
        if probability.compare(scale) != std::cmp::Ordering::Less {
            return 2;
        }
        let mut component = component_count - 1;
        let mut left_edge = U256::ZERO;
        let mut right_edge = U256::ZERO;
        for (index, weight) in weights[..component_count].iter().copied().enumerate() {
            let Some(next_edge) = right_edge.checked_add(weight) else {
                return 2;
            };
            if probability.compare(next_edge) == std::cmp::Ordering::Less {
                component = index;
                left_edge = right_edge;
                break;
            }
            right_edge = next_edge;
        }
        let local = probability.subtract(left_edge);
        let weight = weights[component];
        let mut index = 0_usize;
        while index < quantile_count {
            let Some(left) = local.checked_mul_u32(grid_denominator) else {
                return 2;
            };
            let Some(right) = weight.checked_mul_u32(grid[index]) else {
                return 2;
            };
            if left.compare(right) != std::cmp::Ordering::Greater {
                break;
            }
            index += 1;
        }
        let row = &times[component * quantile_count..(component + 1) * quantile_count];
        let value = if index == 0 {
            row[0]
        } else if index == quantile_count {
            row[quantile_count - 1]
        } else {
            let Some(local_scaled) = local.checked_mul_u32(grid_denominator) else {
                return 2;
            };
            let Some(left_boundary) = weight.checked_mul_u32(grid[index - 1]) else {
                return 2;
            };
            let ratio_numerator = local_scaled.subtract(left_boundary);
            let Some(ratio_denominator) =
                weight.checked_mul_u32(grid[index] - grid[index - 1])
            else {
                return 2;
            };
            let delta = (row[index] - row[index - 1]) as u32;
            let Some(exact_numerator) = ratio_numerator.checked_mul_u32(delta) else {
                return 2;
            };
            let Some((mut offset, remainder)) =
                div_rem_u256_bounded(exact_numerator, ratio_denominator, delta)
            else {
                return 2;
            };
            let Some(doubled) = remainder.checked_mul_u32(2) else {
                return 2;
            };
            if doubled.compare(ratio_denominator) == std::cmp::Ordering::Greater
                || (doubled == ratio_denominator && offset % 2 == 1)
            {
                offset += 1;
            }
            let Some(rounded) = row[index - 1].checked_add(offset as i32) else {
                return 2;
            };
            rounded
        };
        output[draw] = value;
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_optimizer_context_create(
    samples_ptr: *const i32,
    draw_count: usize,
    entrant_count: usize,
) -> *mut std::ffi::c_void {
    if samples_ptr.is_null()
        || draw_count != REQUIRED_DRAWS
        || entrant_count == 0
        || entrant_count > MAX_ENTRANTS
    {
        return std::ptr::null_mut();
    }
    let samples = unsafe {
        std::slice::from_raw_parts(samples_ptr, draw_count * entrant_count).to_vec()
    };
    if samples.iter().any(|sample| !(1..=2_000_000_000).contains(sample)) {
        return std::ptr::null_mut();
    }
    let word_count = draw_count.div_ceil(64);
    let Some(mask_count) = entrant_count
        .checked_mul(entrant_count)
        .and_then(|value| value.checked_mul(THRESHOLD_COUNT))
        .and_then(|value| value.checked_mul(word_count))
    else {
        return std::ptr::null_mut();
    };
    let mut core = EvaluationCore {
        samples,
        draw_count,
        entrant_count,
        word_count,
        comparison_masks: vec![0; mask_count],
    };
    let mut differences = Vec::with_capacity(draw_count);
    let mut cumulative = vec![0_u64; word_count];
    for left in 0..entrant_count {
        for right in 0..entrant_count {
            if left == right {
                continue;
            }
            differences.clear();
            differences.extend((0..draw_count).map(|draw| {
                (
                    core.samples[draw * entrant_count + left]
                        - core.samples[draw * entrant_count + right],
                    draw,
                )
            }));
            differences.sort_unstable_by_key(|item| item.0);
            cumulative.fill(0);
            let mut cursor = 0_usize;
            for threshold_index in 0..THRESHOLD_COUNT {
                let threshold_ms =
                    (threshold_index as i32 - THRESHOLD_OFFSET) * 1_000;
                while cursor < differences.len() && differences[cursor].0 <= threshold_ms {
                    let draw = differences[cursor].1;
                    cumulative[draw / 64] |= 1_u64 << (draw % 64);
                    cursor += 1;
                }
                let offset = core.mask_offset(left, right, threshold_index);
                core.comparison_masks[offset..offset + word_count]
                    .copy_from_slice(&cumulative);
            }
        }
    }
    let core = Arc::new(core);
    let Ok(worker_pool) = WorkerPool::new(&core) else {
        return std::ptr::null_mut();
    };
    let context = EvaluationContext { core, worker_pool };
    Box::into_raw(Box::new(context)).cast()
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_optimizer_context_free(
    context: *mut std::ffi::c_void,
) {
    if !context.is_null() {
        drop(unsafe { Box::from_raw(context.cast::<EvaluationContext>()) });
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_optimizer_evaluate(
    context_ptr: *const std::ffi::c_void,
    delays_ptr: *const i32,
    candidate_count: usize,
    credit_scale: i64,
    spreads_ptr: *mut i64,
    credits_ptr: *mut i64,
) -> i32 {
    if context_ptr.is_null()
        || delays_ptr.is_null()
        || spreads_ptr.is_null()
        || credits_ptr.is_null()
        || candidate_count == 0
        || credit_scale <= 0
    {
        return 1;
    }
    let typed_context = context_ptr.cast::<EvaluationContext>();
    let context = unsafe { &*typed_context };
    let Some(credit_count) = candidate_count.checked_mul(context.core.entrant_count) else {
        return 2;
    };
    unsafe { std::ptr::write_bytes(credits_ptr, 0, credit_count) };
    unsafe {
        context.worker_pool.evaluate(
            delays_ptr,
            candidate_count,
            credit_scale,
            spreads_ptr,
            credits_ptr,
        )
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn strathmark_v3_optimizer_mark_dominated(
    sources_ptr: *const i64,
    source_count: usize,
    targets_ptr: *const i64,
    target_count: usize,
    target_dominated_ptr: *mut u8,
    nonstrict_ptr: *const i64,
    strict_ptr: *const i64,
) -> i32 {
    if sources_ptr.is_null()
        || targets_ptr.is_null()
        || target_dominated_ptr.is_null()
        || nonstrict_ptr.is_null()
        || strict_ptr.is_null()
        || source_count == 0
        || target_count == 0
    {
        return 1;
    }
    if target_count >= 128 {
        let pool = PARETO_POOL.get_or_init(|| ParetoWorkerPool::new().ok());
        if let Some(pool) = pool {
            return unsafe {
                pool.mark(
                    sources_ptr,
                    source_count,
                    targets_ptr,
                    target_count,
                    target_dominated_ptr,
                    nonstrict_ptr,
                    strict_ptr,
                )
            };
        }
    }
    let sources = unsafe { std::slice::from_raw_parts(sources_ptr, source_count * 4) };
    let targets = unsafe { std::slice::from_raw_parts(targets_ptr, target_count * 4) };
    let dominated =
        unsafe { std::slice::from_raw_parts_mut(target_dominated_ptr, target_count) };
    let nonstrict = unsafe { std::slice::from_raw_parts(nonstrict_ptr, 4) };
    let strict = unsafe { std::slice::from_raw_parts(strict_ptr, 4) };
    mark_dominated_rows(sources, targets, dominated, nonstrict, strict);
    0
}

fn mark_dominated_rows(
    sources: &[i64],
    targets: &[i64],
    dominated: &mut [u8],
    nonstrict: &[i64],
    strict: &[i64],
) {
    let source_count = sources.len() / 4;
    for target_index in 0..dominated.len() {
        if dominated[target_index] != 0 {
            continue;
        }
        let target = &targets[target_index * 4..(target_index + 1) * 4];
        for source_index in 0..source_count {
            let source = &sources[source_index * 4..(source_index + 1) * 4];
            let mut any_strict = false;
            for column in 0..4 {
                let source_value = i128::from(source[column]);
                let target_value = i128::from(target[column]);
                if source_value > target_value + i128::from(nonstrict[column]) {
                    any_strict = false;
                    break;
                }
                any_strict |= source_value <= target_value + i128::from(strict[column]);
            }
            if any_strict {
                dominated[target_index] = 1;
                break;
            }
        }
    }
}

unsafe fn evaluate_range(
    context: &EvaluationCore,
    delays_ptr: *const i32,
    start: usize,
    end: usize,
    credit_scale: i64,
    spreads_ptr: *mut i64,
    credits_ptr: *mut i64,
    winner_masks: &mut [u64],
) {
    let entrants = context.entrant_count;
    let words = context.word_count;
    let last_word_mask = if context.draw_count % 64 == 0 {
        u64::MAX
    } else {
        (1_u64 << (context.draw_count % 64)) - 1
    };
    for candidate in start..end {
        let delays = unsafe {
            std::slice::from_raw_parts(delays_ptr.add(candidate * entrants), entrants)
        };
        for left in 0..entrants {
            let target = &mut winner_masks[left * words..(left + 1) * words];
            target.fill(u64::MAX);
            target[words - 1] = last_word_mask;
            for right in 0..entrants {
                if left == right {
                    continue;
                }
                let Some(threshold) = delays[right].checked_sub(delays[left]) else {
                    unsafe { *spreads_ptr.add(candidate) = -1 };
                    return;
                };
                if threshold % 1_000 != 0 || !(-180_000..=180_000).contains(&threshold) {
                    unsafe { *spreads_ptr.add(candidate) = -1 };
                    return;
                }
                let threshold_index = (threshold / 1_000 + THRESHOLD_OFFSET) as usize;
                let offset = context.mask_offset(left, right, threshold_index);
                let source = &context.comparison_masks[offset..offset + words];
                for word in 0..words {
                    target[word] &= source[word];
                }
            }
        }

        let credits = unsafe {
            std::slice::from_raw_parts_mut(
                credits_ptr.add(candidate * entrants),
                entrants,
            )
        };
        let mut tie_words = [0_u64; REQUIRED_DRAWS / 64];
        let mut union_words = [0_u64; REQUIRED_DRAWS / 64];
        for entrant in 0..entrants {
            let mask = &winner_masks[entrant * words..(entrant + 1) * words];
            for word in 0..words {
                tie_words[word] |= union_words[word] & mask[word];
                union_words[word] |= mask[word];
            }
        }
        if union_words[..words - 1].iter().any(|word| *word != u64::MAX)
            || union_words[words - 1] != last_word_mask
        {
            unsafe { *spreads_ptr.add(candidate) = -1 };
            return;
        }
        for entrant in 0..entrants {
            let mask = &winner_masks[entrant * words..(entrant + 1) * words];
            let mut unique_count = 0_u32;
            for word in 0..words {
                unique_count += (mask[word] & !tie_words[word]).count_ones();
            }
            credits[entrant] += i64::from(unique_count) * credit_scale;
        }
        for (word_index, tie_word) in tie_words.iter().copied().enumerate() {
            let mut remaining = tie_word;
            while remaining != 0 {
                let bit = remaining.trailing_zeros() as usize;
                let bit_mask = 1_u64 << bit;
                let winner_count = (0..entrants)
                    .filter(|entrant| {
                        winner_masks[*entrant * words + word_index] & bit_mask != 0
                    })
                    .count();
                if winner_count < 2 {
                    unsafe { *spreads_ptr.add(candidate) = -1 };
                    return;
                }
                let winner_credit = credit_scale / winner_count as i64;
                for entrant in 0..entrants {
                    if winner_masks[entrant * words + word_index] & bit_mask != 0 {
                        credits[entrant] += winner_credit;
                    }
                }
                remaining &= remaining - 1;
            }
        }
        let mut spread_sum = 0_i64;
        for draw in 0..context.draw_count {
            let draw_samples =
                &context.samples[draw * entrants..(draw + 1) * entrants];
            let mut minimum = i64::MAX;
            let mut maximum = i64::MIN;
            for entrant in 0..entrants {
                let finish = i64::from(draw_samples[entrant]) + i64::from(delays[entrant]);
                minimum = minimum.min(finish);
                maximum = maximum.max(finish);
            }
            spread_sum += maximum - minimum;
        }
        unsafe { *spreads_ptr.add(candidate) = spread_sum };
    }
}

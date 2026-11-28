from threading import Thread, Event
from queue import Queue
import time
import numpy as np
import traceback

# This code originally used Process not Thread.
# Process is much slower to start (Process.start() is ~100 ms, Thread.start() is a few ms)
# The process-safe versions of Queue and Event are also significantly slower.
# On the other hand, CPU-bound Python threads can't run in parallel ("global interpreter lock").
# The overall problem is not CPU-bound - we should always be limited by tProc execution.
# In the worst case where the tProc is running fast, we should actually be waiting for IO a lot (due to the DMA).
# So we think it's safe to use threads.
# However, this is a complicated problem and we may ultimately need to mess around with sys.setswitchinterval() or go back to Process.
# To use Process instead of Thread, use the following import and change WORKERTYPE.
#from multiprocessing import Process, Queue, Event

class DataStreamer():
    """
    Uses a separate thread to read data from the average buffers.
    The class methods define the readout loop and initialization of the worker thread.
    The QickSoc methods start_readout() and poll_data() are the external interface to the streamer.

    We don't lock the QickSoc or the IPs. The user is responsible for not disrupting a readout in progress.

    :param soc: The QickSoc object.
    :type soc: QickSoc
    """

    #WORKERTYPE = Process
    WORKERTYPE = Thread

    def __init__(self, soc):
        self.soc = soc

        self.start_worker()

    def start_worker(self):
        # Initialize flags and queues.
        # Passes run commands from the main thread to the worker thread.
        self.job_queue = Queue()
        # Passes data from the worker thread to the main thread.
        self.data_queue = Queue()
        # Passes exceptions from the worker thread to the main thread.
        self.error_queue = Queue()
        # The main thread can use this flag to tell the worker thread to stop.
        # The main thread clears the flag when starting readout.
        self.stop_flag = Event()
        # The worker thread uses this to tell the main thread when it's done.
        # The main thread clears the flag when starting readout.
        self.done_flag = Event()
        self.done_flag.set()

        # Process object for the streaming readout.
        # daemon=True means the readout thread will be killed if the parent is killed
        self.readout_worker = self.WORKERTYPE(target=self._run_readout, daemon=True)
        self.readout_worker.start()

    def stop_readout(self):
        """
        Signal the readout loop to break.
        """
        self.stop_flag.set()

    def readout_running(self):
        """
        Test if the readout loop is running.

        :return: readout thread status
        :rtype: bool
        """
        return not self.done_flag.is_set()

    def data_available(self):
        """
        Test if data is available in the queue.

        :return: data queue status
        :rtype: bool
        """
        return not self.data_queue.empty()

    def _run_readout(self):
        """
        Worker thread for the streaming readout

        :param total_count: Number of data points expected
        :type addr: int
        :param counter_addr: Data memory address for the loop counter
        :type counter_addr: int
        :param ch_list: List of readout channels
        :type addr: list of int
        :param reads_per_count: Number of data points to expect per counter increment
        :type reads_per_count: list of int
        """
        while True:
            try:
                # wait for a job
                total_shots, counter_addr, ch_list, reads_per_count, stride = self.job_queue.get(block=True)
                #print("streamer loop: start", total_count)

                # the drain loop itself is iter_accumulated() (shared with the board server);
                # this thread keeps its old behaviour of failing on a ring overrun
                for item in iter_accumulated(self.soc, total_shots, counter_addr, ch_list,
                                             reads_per_count, stride=stride, stop_flag=self.stop_flag):
                    if item[0] == 'stopped':
                        print("streamer loop: got stop flag")
                    elif item[0] == 'overrun':
                        _, first, last, info = item
                        raise RuntimeError("Overflowed the averages buffer (%d unread samples >= buffer size %d)."
                                           % (info['unread_points'], info['avg_maxlen']) +
                                           "\nYou need to slow down the tProc by increasing relax_delay." +
                                           "\nIf the TQDM progress bar is enabled, disabling it may help.")
                    else:
                        _, first, last, acc_buf, stats = item
                        self.data_queue.put((last - first, (acc_buf, stats)))
                #if last_count==total_count: print("streamer loop: normal completion")

            except Exception as e:
                print("streamer loop: got exception")
                # traceback.print_exc()
                # pass the exception to the main thread
                self.error_queue.put(e)
                # put dummy data in the data queue, to trigger a poll_data read
                self.data_queue.put((0, (None, None)))
            finally:
                # we should set the done flag regardless of whether we completed readout, used the stop flag, or errored out
                self.done_flag.set()
                # set tproc for internal start so we don't run the program repeatedly (this also clears the internal-start register)
                self.soc.start_src("internal")


def _overwritten_before(counter, reads_per_shot, maxlens):
    """First shot whose accumulated data is still intact in every ring.

    The ring of a readout holds maxlen points; point q is overwritten by point
    q + maxlen. With the shot counter at `counter`, points up to (counter+1)*n - 1
    may have been written (the shot in progress included, to be safe), so a shot s
    is intact only if its first point s*n is above (counter+1)*n - 1 - maxlen.
    """
    first_ok = 0
    for n, maxlen in zip(reads_per_shot, maxlens):
        bad_last_point = (counter + 1) * n - 1 - maxlen
        if bad_last_point >= 0:
            first_ok = max(first_ok, bad_last_point // n + 1)
    return first_ok


def iter_accumulated(soc, total_shots, counter_addr, ch_list, reads_per_shot, stride=None,
                     stop_flag=None, poll_interval=0.0, lock=None, deadline=None):
    """Drain the accumulated (one value per readout) ring buffers while the tProc runs.

    Starts the tProc (with internal start; with external start it waits for the
    trigger), then follows the tProc shot counter and reads new shots from each
    readout's ring. Yields, in shot order:

    * ('data', first, last, bufs, stats): shots [first, last) of this run; bufs[i]
      is the raw (I, Q) array of ch_list[i], shape ((last-first)*reads_per_shot[i], 2).
    * ('overrun', first, last, info): shots [first, last) were (or may have been)
      overwritten in the firmware ring before they were read; their data is not
      delivered. Checked against the counter both before and after each transfer.
    * ('stopped', shots_read): stop_flag was set, or the deadline (time.monotonic)
      passed; nothing more follows.

    At the end the start source is set back to internal, as DataStreamer did.
    poll_interval: seconds to sleep between counter reads that found nothing new
    (0 spins like the original DataStreamer). lock: held around each counter read
    and transfer, so another thread can read tProc registers in between.
    """
    if lock is None:
        lock = _NullLock()
    if isinstance(reads_per_shot, int):
        reads_per_shot = [reads_per_shot] * len(ch_list)
    maxlens = [soc['readouts'][ch]['avg_maxlen'] for ch in ch_list]
    # largest number of shots one transfer may cover: transfer_avg needs length < avg_maxlen
    max_span = min((maxlen - 1) // n for maxlen, n in zip(maxlens, reads_per_shot) if n > 0) \
        if any(n > 0 for n in reads_per_shot) else total_shots
    max_span = max(1, max_span)
    if stride is None:
        # 10% of the smallest buffer, at least 1 shot (as DataStreamer did)
        shots_fit = [maxlen / n for maxlen, n in zip(maxlens, reads_per_shot) if n > 0]
        stride = max(1, int(0.1 * min(shots_fit))) if shots_fit else max(1, total_shots)
    stride = max(1, min(stride, max_span))

    t_start = time.time()
    try:
        with lock:
            soc.start_tproc()
        last = 0
        while last < total_shots:
            if stop_flag is not None and stop_flag.is_set():
                yield ('stopped', last)
                return
            if deadline is not None and time.monotonic() > deadline:
                yield ('stopped', last)
                return
            with lock:
                shots = soc.get_tproc_counter(addr=counter_addr)
            if shots < min(last + stride, total_shots):
                if poll_interval:
                    time.sleep(poll_interval)
                continue
            shots = min(shots, total_shots)
            # shots already lost before we read anything (once the program has finished,
            # nothing is in progress, hence total_shots - 1)
            first_ok = _overwritten_before(min(shots, total_shots - 1), reads_per_shot, maxlens)
            if first_ok > last:
                lost_to = min(first_ok, shots)
                unread = max((shots - last) * n for n in reads_per_shot)
                yield ('overrun', last, lost_to, {'when': 'before_read', 'counter': shots,
                                                  'unread_points': unread, 'avg_maxlen': min(maxlens)})
                last = lost_to
                if last >= shots:
                    continue
            end = min(shots, last + max_span)
            bufs = []
            with lock:
                for ch, n in zip(ch_list, reads_per_shot):
                    addr = (last * n) % soc['readouts'][ch]['avg_maxlen']
                    bufs.append(soc.get_accumulated(ch=ch, address=addr, length=(end - last) * n))
                after = soc.get_tproc_counter(addr=counter_addr) if end < total_shots else total_shots
            # shots that may have been overwritten while the transfer ran (the program may
            # have finished meanwhile: then it wrote up to total_shots and stopped)
            first_ok = _overwritten_before(min(after, total_shots - 1), reads_per_shot, maxlens)
            if first_ok > last:
                bad_to = min(first_ok, end)
                yield ('overrun', last, bad_to, {'when': 'during_read', 'counter': after,
                                                 'unread_points': max((after - last) * n for n in reads_per_shot),
                                                 'avg_maxlen': min(maxlens)})
                k = bad_to - last
                bufs = [b[k * n:] for b, n in zip(bufs, reads_per_shot)]
                last = bad_to
            if end > last:
                stats = (time.time() - t_start, shots, (last * reads_per_shot[-1]) % maxlens[-1] if maxlens else 0,
                         end - last)
                yield ('data', last, end, bufs, stats)
            last = end
    finally:
        with lock:
            # internal start, so the program does not run again (this also clears the internal-start register)
            soc.start_src("internal")


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
